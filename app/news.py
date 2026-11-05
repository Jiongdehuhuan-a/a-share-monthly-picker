# -*- coding: utf-8 -*-
"""
新闻情绪模块

三条数据线
    1. 市场快讯   newsapi.eastmoney.com      → 大盘情绪指数
    2. 个股新闻   search-api-web.eastmoney.com → 个股情绪分
    3. 情绪判定   中文财经词典 + 否定/程度修饰 + 硬利空事件识别

设计原则
    · 只做「情绪」不做「事实」，判不准的新闻计 0 分，宁缺毋滥
    · 硬利空（立案调查、退市风险、财务造假…）直接一票否决
    · 情绪只作为一个有界倾斜项叠加，不改变原有因子结构
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from . import config, storage

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MARKET_URL = ("https://newsapi.eastmoney.com/kuaixun/v1/"
              "getlist_102_ajaxResult_50_{page}_.html")
SINA_URL = ("https://zhibo.sina.com.cn/api/zhibo/feed"
            "?zhibo_id=152&page=1&page_size=30&tag_id=0&dire=f&dpc=1")
SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="

# ------------------------------------------------------------------ 词典
# 权重 1.0 为常规，1.5 为强信号
POSITIVE = {
    "业绩预增": 1.5, "预增": 1.2, "扭亏": 1.5, "扭亏为盈": 1.6, "超预期": 1.5,
    "超出预期": 1.5, "创新高": 1.4, "历史新高": 1.4, "净利润增长": 1.2,
    "营收增长": 1.1, "同比增长": 0.9, "大幅增长": 1.2, "快速增长": 1.1,
    "中标": 1.4, "中签": 0.4, "签订合同": 1.2, "签署协议": 1.1, "重大合同": 1.2,
    "战略合作": 1.2, "达成合作": 1.1, "收购": 1.0, "并购": 0.9,
    "重组": 1.1, "资产注入": 1.3, "借壳": 1.2, "增持": 1.3, "回购": 1.2,
    "分红": 0.9, "派息": 0.8, "高送转": 1.0, "提价": 1.2, "涨价": 1.3,
    "产品涨价": 1.4, "订单": 1.0, "大单": 1.2, "获批": 1.3, "批准": 1.0,
    "通过审批": 1.3, "注册获批": 1.3, "投产": 1.1, "量产": 1.2,
    "扩产": 1.0, "满产": 1.1, "供不应求": 1.3, "爆发": 0.8, "突破": 1.0,
    "利好": 1.2, "补贴": 0.9, "减税": 1.0, "降准": 1.1, "降息": 1.1,
    "松绑": 1.0, "扶持": 0.9, "政策支持": 1.1, "国产替代": 1.1,
    "龙头": 0.7, "领先": 0.7, "独家": 1.0, "首单": 0.9, "破发": -1.0,
    "举牌": 1.2, "股权激励": 1.0, "员工持股": 0.9, "引入战投": 1.1,
    "涨停": 0.8, "大涨": 0.9, "反弹": 0.4, "回升": 0.6,
    "改善": 0.8, "转暖": 0.8, "复苏": 0.9, "回暖": 0.8,
    "盈利": 0.8, "毛利率提升": 1.2, "降本增效": 0.9, "成本下降": 1.0,
    "获得认证": 1.0, "入选": 0.8, "获奖": 0.5, "成功": 0.6,
}

NEGATIVE = {
    "业绩预减": 1.5, "预减": 1.2, "预亏": 1.6, "亏损": 1.4, "由盈转亏": 1.6,
    "不及预期": 1.5, "低于预期": 1.5, "下滑": 1.1, "大幅下滑": 1.4,
    "同比下降": 1.1, "承压": 1.0, "恶化": 1.3,
    "减持": 1.3, "清仓": 1.2, "套现": 1.1, "质押": 1.0, "爆仓": 1.6,
    "平仓": 1.2, "违约": 1.6, "逾期": 1.3, "诉讼": 1.1, "仲裁": 1.0,
    "处罚": 1.4, "罚款": 1.2, "警示函": 1.3, "监管函": 1.2,
    "问询函": 1.0, "关注函": 0.9, "立案": 1.8, "调查": 1.1, "违规": 1.3,
    "造假": 1.9, "退市": 1.9, "终止上市": 2.0, "暂停上市": 1.8,
    "终止": 1.0, "失败": 1.1, "取消": 0.9, "搁浅": 1.0, "下调": 1.1,
    "减产": 1.1, "停产": 1.5, "停工": 1.3, "关闭": 0.9, "裁员": 1.1,
    "破产": 1.8, "重整": 1.2, "债务": 1.0, "资不抵债": 1.8,
    "商誉减值": 1.6, "计提": 1.1, "减值": 1.3, "坏账": 1.4,
    "解禁": 0.9, "抛售": 1.2, "跌停": 0.9, "大跌": 1.0,
    "终止重组": 1.8, "终止收购": 1.6, "重组失败": 1.8, "收购失败": 1.6,
    "重组终止": 1.8, "终止定增": 1.3, "终止合作": 1.3, "合作终止": 1.3,
    "取消订单": 1.4, "订单取消": 1.4, "项目终止": 1.4,
    "暴跌": 1.2, "跳水": 1.0, "新低": 1.1, "创新低": 1.3,
    "利空": 1.2, "事故": 1.4, "召回": 1.3, "安全事故": 1.6,
    "约谈": 1.1, "整改": 0.9, "查封": 1.4, "冻结": 1.3, "失信": 1.3,
    "降价": 1.0, "价格战": 1.2, "竞争加剧": 1.1, "需求疲软": 1.3,
    "产能过剩": 1.1, "库存高企": 1.0, "毛利率下滑": 1.3,
    "成本上升": 1.0, "汇兑损失": 0.9, "计提减值": 1.4,
}

# 硬利空：命中即从候选池剔除
HARD_NEGATIVE = [
    "立案调查", "被立案", "立案侦查", "退市风险", "终止上市", "暂停上市",
    "财务造假", "虚增", "资金占用", "违规担保", "破产重整", "被逮捕",
    "被留置", "债务违约", "无法表示意见", "否定意见", "内幕交易",
    "操纵市场", "实控人被", "董事长被", "业绩变脸", "商誉暴雷",
    "立案告知", "涉嫌信息披露违法违规",
]

NEGATORS = ["不", "未", "无", "非", "没有", "并未", "尚未", "否认", "不再",
            "停止", "暂无", "不会", "难以"]
DEGREE_UP = {"大幅": 1.5, "显著": 1.4, "明显": 1.3, "急剧": 1.5, "暴": 1.4}
DEGREE_DOWN = {"略": 0.6, "小幅": 0.6, "轻微": 0.5, "或将": 0.7, "有望": 0.8}

# 标题里出现这些词，说明是基金/指数/盘面综述，不是个股新闻，一律跳过
GENERIC_TITLE = ("ETF", "基金", "净值", "收评", "午评", "早评", "盘面",
                 "龙虎榜", "涨幅榜", "主力资金", "板块异动")

# 上下文规则：同一个词在不同搭配里方向相反（"减值损失减少"是利好，
# "净利润减少"是利空），用几条可解释的正则兜底。
RULES = [
    (re.compile(r"净利.{0,8}(增|涨|提升)"), 1.2),
    (re.compile(r"(净利|利润).{0,8}(降|减|亏损|亏)"), -1.2),
    (re.compile(r"(费用|成本|负债).{0,6}(下降|减少|降低)"), 1.0),
    (re.compile(r"(费用|成本|负债).{0,6}(上升|增加|攀升)"), -1.0),
    (re.compile(r"毛利.{0,6}(提升|增加|上升|提高)"), 1.0),
    (re.compile(r"毛利.{0,6}(下滑|下降|降低)"), -1.0),
    (re.compile(r"营收.{0,8}(增长|增加|提升)"), 0.8),
    (re.compile(r"营收.{0,8}(下滑|下降|减少)"), -0.8),
    (re.compile(r"签订.{0,12}合同"), 1.0),
    (re.compile(r"中标.{0,10}项目"), 1.0),
]


def norm(s: str) -> str:
    """全角转半角、去空格，用于名称匹配。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"\s+", "", s).upper()


def relevant(name: str, title: str) -> bool:
    """个股新闻判定：标题里必须出现公司名，且不是基金/盘面综述。"""
    n, t = norm(name), norm(title)
    if len(n) < 2 or n not in t:
        return False
    return not any(g.upper() in t for g in GENERIC_TITLE)


def _term_pattern(terms):
    keys = sorted(terms, key=len, reverse=True)
    return re.compile("|".join(re.escape(k) for k in keys))


_POS_RE = _term_pattern(POSITIVE)
_NEG_RE = _term_pattern(NEGATIVE)
_HARD_RE = re.compile("|".join(re.escape(k) for k in HARD_NEGATIVE))


def classify(text: str, is_title: bool = False) -> dict:
    """返回 {'score': float, 'hard': bool, 'pos': [...], 'neg': [...]}"""
    if not text:
        return {"score": 0.0, "hard": False, "pos": [], "neg": []}
    text = re.sub(r"\s+", "", text)
    score = 0.0
    pos_hits, neg_hits = [], []
    used = [False] * len(text)

    for m in _POS_RE.finditer(text):
        s, e = m.span()
        if any(used[s:e]):
            continue
        w = POSITIVE[m.group()]
        ctx = text[max(0, s - 6):s]
        sign = 1.0
        if any(n in ctx for n in NEGATORS):
            sign = -0.7
        for d, multi in DEGREE_UP.items():
            if d in ctx:
                w *= multi
                break
        for d, multi in DEGREE_DOWN.items():
            if d in ctx:
                w *= multi
                break
        score += sign * w
        pos_hits.append(m.group())
        for i in range(s, e):
            used[i] = True

    for m in _NEG_RE.finditer(text):
        s, e = m.span()
        if any(used[s:e]):
            continue
        w = NEGATIVE[m.group()]
        ctx = text[max(0, s - 6):s]
        sign = -1.0
        if any(n in ctx for n in NEGATORS):
            sign = 0.6
        for d, multi in DEGREE_UP.items():
            if d in ctx:
                w *= multi
                break
        score += sign * w
        neg_hits.append(m.group())
        for i in range(s, e):
            used[i] = True

    # 上下文规则兜底（0.6 权重，避免与词典重复计数过度）
    score += 0.6 * sum(w for pat, w in RULES if pat.search(text))
    if is_title:
        score *= 1.6
    score = max(-3.0, min(3.0, score / max(1.0, math.sqrt(len(text) / 18.0))))
    return {"score": round(score, 3), "hard": bool(_HARD_RE.search(text)),
            "pos": pos_hits[:4], "neg": neg_hits[:4]}


# ------------------------------------------------------------------ 抓取
def _http(url: str, referer: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": referer})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def fetch_market_feed(pages: int = 3) -> list[dict]:
    out = []
    for p in range(1, pages + 1):
        try:
            t = _http(MARKET_URL.format(page=p), "https://news.eastmoney.com/")
            j = json.loads(t.split("=", 1)[1])
            for it in j.get("LivesList", []):
                out.append({
                    "id": str(it.get("id") or it.get("newsid") or it.get("sort")),
                    "time": it.get("showtime", ""),
                    "title": it.get("title", "") or "",
                    "digest": it.get("digest", "") or "",
                    "url": it.get("url_w", ""),
                })
        except Exception as e:  # noqa: BLE001
            storage.log(f"market feed error: {e}")
    return out


def _clean(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").replace("\u3000", " ").strip()


def fetch_sina_flash(size: int = 30) -> list[dict]:
    """新浪财经 7x24 直播——比东财快讯快 3-5 分钟。"""
    try:
        t = _http(SINA_URL, "https://finance.sina.com.cn/7x24/")
        j = json.loads(t)
        lst = j["result"]["data"]["feed"]["list"]
    except Exception as e:  # noqa: BLE001
        storage.log(f"sina feed error: {e}")
        return []
    out = []
    for it in lst[:size]:
        txt = _clean(it.get("rich_text") or "")
        if not txt:
            continue
        out.append({
            "id": "sina_" + str(it.get("id") or it.get("zhibo_id") or txt[:16]),
            "time": it.get("create_time", ""),
            "title": txt[:200],
            "digest": "",
            "url": "",
            "src": "新浪",
        })
    return out


# ------------------------------------------------------------------ 实时流
LIVE = {"items": [], "index": 0.0, "pos": 0, "neg": 0, "n": 0,
        "updated": None, "last_poll": 0.0, "error": None,
        "newest": "", "sources": {}}
_LIVE_LOCK = threading.Lock()
_KEYWORDS = ("涨停", "跌停", "发布", "公告", "中标", "增持", "减持", "回购",
             "停牌", "复牌", "解禁", "重组", "立案", "处罚", "问询")


def _score_item(x: dict) -> dict:
    c = classify((x["title"] or "") + " " + (x["digest"] or "")[:60])
    x["score"] = c["score"]
    x["hard"] = c["hard"]
    return x


def poll_once() -> dict:
    """拉一次快讯并合并进内存缓冲（东财 + 新浪双源），重算情绪指数。"""
    raw = []
    srcs = {}
    try:
        t = _http(MARKET_URL.format(page=1), "https://news.eastmoney.com/")
        j = json.loads(t.split("=", 1)[1])
        for it in j.get("LivesList", []):
            raw.append({
                "id": str(it.get("id") or it.get("newsid") or it.get("sort")),
                "time": it.get("showtime", ""),
                "title": it.get("title", "") or "",
                "digest": it.get("digest", "") or "",
                "url": it.get("url_w", ""),
                "src": "东财",
            })
        srcs["东财"] = len(raw)
    except Exception as e:  # noqa: BLE001
        LIVE["error"] = str(e)
        srcs["东财"] = 0
    sina = fetch_sina_flash()
    srcs["新浪"] = len(sina)
    raw += sina
    if not raw:
        return LIVE

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    with _LIVE_LOCK:
        buf = {x["id"]: x for x in LIVE["items"]}
        clamped = 0
        for it in raw:
            if it["id"] in buf:
                continue
            # 两家的快讯都会带「未来 1~9 分钟」的预发布时间戳（新浪尤其明显）。
            # 直接丢弃会漏掉最快的那一路信号，所以保留条目、把时间对齐到当前时刻。
            if it.get("time", "") > now_str:
                it["time"] = now_str
                clamped += 1
            buf[it["id"]] = _score_item(it)
        cutoff = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        arr = [x for x in buf.values() if x["time"] >= cutoff]
        arr.sort(key=lambda x: x["time"], reverse=True)
        arr = arr[:500]
        num = den = 0.0
        pos = neg = 0
        for x in arr:
            age = _age_days(x["time"])
            w = math.exp(-age / 1.5)
            num += w * x.get("score", 0.0)
            den += w
            if x.get("score", 0) > 0.3:
                pos += 1
            elif x.get("score", 0) < -0.3:
                neg += 1
        LIVE.update(items=arr, n=len(arr), pos=pos, neg=neg,
                    index=round(max(-1.0, min(1.0, (num / den if den else 0) / 2)), 3),
                    updated=datetime.now().strftime("%H:%M:%S"),
                    newest=arr[0]["time"] if arr else "",
                    sources=srcs, deferred=clamped,
                    last_poll=time.time(), error=None)
    return LIVE


def start_live_loop(interval: int = 60, stop_flag: dict | None = None):
    def loop():
        while not (stop_flag or {}).get("stop"):
            try:
                poll_once()
            except Exception as e:  # noqa: BLE001
                LIVE["error"] = str(e)
                storage.log(f"live poll error: {e}")
            for _ in range(interval):
                if (stop_flag or {}).get("stop"):
                    return
                time.sleep(1)

    threading.Thread(target=loop, name="news-live", daemon=True).start()
    storage.log(f"news live loop started, interval={interval}s")


def live_payload(holding_names: list[str] | None = None,
                 limit: int = 60) -> dict:
    names = [n for n in (holding_names or []) if len(str(n)) >= 3]
    with _LIVE_LOCK:
        items = LIVE["items"][:limit]
        out = []
        for x in items:
            t = norm(x["title"])
            hit = next((n for n in names if norm(n) in t), None)
            out.append({"id": x["id"], "time": x["time"],
                        "title": x["title"], "score": x.get("score", 0.0),
                        "hard": x.get("hard", False), "src": x.get("src", ""),
                        "url": x.get("url", ""), "hit": hit})
        return {"index": LIVE["index"], "pos": LIVE["pos"], "neg": LIVE["neg"],
                "n": LIVE["n"], "updated": LIVE["updated"],
                "newest": LIVE.get("newest", ""),
                "sources": LIVE.get("sources", {}),
                "deferred": LIVE.get("deferred", 0),
                "last_poll": LIVE["last_poll"], "error": LIVE["error"],
                "v": config.APP_VERSION,
                "items": out}


def fetch_stock_news(name: str, size: int = 8) -> list[dict]:
    param = {
        "uid": "", "keyword": name, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "time", "pageIndex": 1,
            "pageSize": size, "preTag": "", "postTag": ""}},
    }
    url = SEARCH_URL + urllib.parse.quote(json.dumps(param, ensure_ascii=False))
    try:
        t = _http(url, "https://so.eastmoney.com/")
        body = t[t.index("(") + 1:t.rindex(")")]
        arts = json.loads(body)["result"]["cmsArticleWebOld"]
    except Exception:  # noqa: BLE001
        return []
    out = []
    for a in arts or []:
        out.append({
            "time": a.get("date", ""),
            "title": re.sub("<.*?>", "", a.get("title", "")),
            "content": re.sub("<.*?>", "", a.get("content", "")),
            "media": a.get("mediaName", ""),
            "url": a.get("url", ""),
        })
    return out


# ------------------------------------------------------------------ 打分
def _age_days(t: str) -> float:
    try:
        dt = datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return 30.0
    return max(0.0, (datetime.now() - dt).total_seconds() / 86400.0)


def score_stock(name: str, news: list[dict], max_days: int = 30) -> dict:
    num = den = 0.0
    hard = False
    items = []
    for n in news:
        age = _age_days(n["time"])
        if age > max_days:
            continue
        text = n["title"]
        if not relevant(name, text):
            continue
        c = classify(text, is_title=True)
        if c["hard"]:
            hard = True
        w = math.exp(-age / 10.0)
        num += w * c["score"]
        den += w
        if abs(c["score"]) >= 0.4:
            items.append({"time": n["time"][:16], "title": text[:60],
                          "score": c["score"], "url": n.get("url", "")})
    s = num / den if den > 0 else 0.0
    return {
        "score": round(s, 3),
        "n": len(items),
        "hard": hard,
        "pos": sum(1 for x in items if x["score"] > 0),
        "neg": sum(1 for x in items if x["score"] < 0),
        "items": sorted(items, key=lambda x: x["time"], reverse=True)[:5],
    }


def market_sentiment(feed: list[dict] | None = None) -> dict:
    feed = feed if feed is not None else fetch_market_feed(3)
    num = den = 0.0
    pos = neg = 0
    for it in feed:
        age = _age_days(it["time"])
        if age > 3:
            continue
        c = classify((it["title"] or "") + " " + (it["digest"] or "")[:60])
        w = math.exp(-age / 1.5)
        num += w * c["score"]
        den += w
        if c["score"] > 0.3:
            pos += 1
        elif c["score"] < -0.3:
            neg += 1
    raw = num / den if den > 0 else 0.0
    idx = max(-1.0, min(1.0, raw / 2.0))
    return {
        "index": round(idx, 3),
        "raw": round(raw, 3),
        "n": int(den and round(den)),
        "pos": pos, "neg": neg,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "headlines": [
            {"time": x["time"][:16], "title": x["title"][:60],
             "score": classify(x["title"], True)["score"]}
            for x in feed[:24]
        ],
    }


# ------------------------------------------------------------------ 汇总
CACHE = config.RUNTIME / "news_cache.json"


def build(codes_names: dict[str, str], force: bool = False,
          ttl_min: int = 30, progress_cb=None) -> dict:
    """codes_names: {code: name}。返回 {market, stocks, built}。"""
    cb = progress_cb or (lambda p, m="": None)
    if not force and CACHE.exists():
        try:
            old = json.loads(CACHE.read_text(encoding="utf-8"))
            age = (time.time() - old.get("ts", 0)) / 60
            if age < ttl_min and set(old.get("codes", [])) >= set(codes_names):
                return {**old, "cached": True}
        except Exception:  # noqa: BLE001
            pass

    cb(0.05, "抓取市场快讯…")
    feed = fetch_market_feed(3)
    mk = market_sentiment(feed)

    items = list(codes_names.items())
    out, done = {}, 0
    total = len(items)

    def one(pair):
        code, name = pair
        if not name:
            return code, None
        return code, score_stock(name, fetch_stock_news(name, 8))

    cb(0.15, f"抓取个股新闻 {total} 只…")
    with ThreadPoolExecutor(max_workers=8) as ex:
        for code, res in ex.map(one, items):
            done += 1
            if res is not None:
                out[code] = res
            if done % 20 == 0:
                cb(0.15 + 0.8 * done / max(1, total), f"{done}/{total}")

    # z 只在「有新闻」的样本内标准化；没新闻记 0（中性）。
    # 否则「有新闻」本身会变成一个正向信号。
    scored = [v["score"] for v in out.values() if v.get("n", 0) > 0]
    mu = sum(scored) / len(scored) if scored else 0.0
    sd = ((sum((x - mu) ** 2 for x in scored) / len(scored)) ** 0.5
          if len(scored) > 2 else 1.0)
    for v in out.values():
        if v.get("n", 0) <= 0 or sd <= 1e-6:
            v["z"] = 0.0
        else:
            v["z"] = round((v["score"] - mu) / sd, 3)

    data = {"market": mk, "stocks": out, "built": mk["updated"],
            "codes": list(out.keys()), "ts": time.time(), "cached": False}
    try:
        CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    cb(1.0, "完成")
    storage.log(f"news built: {len(out)} stocks, market={mk['index']}")
    return data
