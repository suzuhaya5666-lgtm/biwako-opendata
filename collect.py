#!/usr/bin/env python3
"""川の防災情報(国土交通省)から観測値を取得し、data/observations.csv に追記する。

使い方:
  python collect.py                      # 取得して CSV に追記
  python collect.py --dump stg 2203900400009   # 1地点の生データを表示(項目の確認用)
出典: 国土交通省 川の防災情報
"""
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://www.river.go.jp/kawabou/file"
OUT = Path("data/observations.csv")
HEADER = ["time", "place", "level", "flow", "temp"]

# (地点名, 種別, 観測所コード, 列名, 値の項目名)
#   種別 stg  = 水位(単位 m、琵琶湖基準水位 B.S.L からの差)
#   種別 qwtr = 水質(wtrtmp が水温 ℃)
# 放水量・安曇川沖の水温は、データの場所がわかったらここに1行ずつ足す。
STATIONS = [
    ("雄琴沖", "stg", "2203900400009", "level", "stg"),   # 取得を確認済み
    ("鳥居川", "stg", "2203900400003", "level", "stg"),   # 同じ形式のはず(未確認)
    ("琵琶湖(平均)", "stg", "2203900400025", "level", "stg"),  # 同上(未確認)
    ("雄琴沖", "qwtr", "2203900600002", "temp", "wtrtmp"),     # 取得を確認済み(雄琴沖中央)
    ("唐橋(瀬田川)", "qwtr", "2203900600001", "temp", "wtrtmp"),  # 同じ形式のはず(未確認)
    ("安曇川沖", "qwtr", "2203900600006", "temp", "wtrtmp"),   # 安曇川沖中央。観測所は確認済み
]


# 天気の記録(日ごとの天気コード・降水量の合計など)。出典: Open-Meteo
W_OUT = Path("data/weather_daily.csv")
W_HEAD = ["date", "place", "code", "precip", "wmax", "tmax", "tmin"]
W_PLACES = [
    ("塩津", 35.5023836, 136.1669634),
    ("海津大崎", 35.4482187, 136.0915766),
    ("姉川", 35.3876593, 136.2155335),
    ("彦根", 35.285681, 136.2464279),
    ("沖島", 35.2024571, 136.0636766),
    ("安曇川", 35.3209525, 136.0780844),
    ("志賀", 35.2008873, 135.9264253),
    ("琵琶湖大橋", 35.1211542, 135.9355907),
    ("烏丸半島", 35.0715445, 135.9326148),
    ("井筒", 35.0544148, 135.8793787),
    ("プリンスホテル前", 35.005424, 135.8914975),
]
W_VARS = "weather_code,precipitation_sum,wind_speed_10m_max,temperature_2m_max,temperature_2m_min"


def collect_weather():
    """日ごとの天気を data/weather_daily.csv にためる。
    毎回、直近3日ぶんを取り直して上書きする(その日の最終値に確定していく)。
    ファイルが新しいときは、過去90日ぶんも一度にさかのぼって取り込む。"""
    rows = {}
    if W_OUT.exists():
        with W_OUT.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[(r["date"], r["place"])] = r
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).date()
    lat = ",".join(str(p[1]) for p in W_PLACES)
    lon = ",".join(str(p[2]) for p in W_PLACES)
    common = f"latitude={lat}&longitude={lon}&daily={W_VARS}&timezone=Asia%2FTokyo&wind_speed_unit=ms"
    urls = []
    days = {}
    for d, pl in rows:
        days.setdefault(pl, set()).add(d)
    if min((len(days.get(pl[0], ())) for pl in W_PLACES), default=0) < 30:  # 記録の少ない地点があれば過去にさかのぼる(確定値・数日おくれ)
        a, b = today - timedelta(days=90), today - timedelta(days=3)
        urls.append(f"https://archive-api.open-meteo.com/v1/archive?{common}&start_date={a}&end_date={b}")
    urls.append(f"https://api.open-meteo.com/v1/forecast?{common}&past_days=2&forecast_days=1")  # 後のものが優先
    for u in urls:
        try:
            data = get_json(u)
            data = data if isinstance(data, list) else [data]
            for (place, _, _), loc in zip(W_PLACES, data):
                d = loc["daily"]
                for k, day in enumerate(d["time"]):
                    code = d["weather_code"][k]
                    pr = d["precipitation_sum"][k]
                    if code is None and pr is None:
                        continue
                    g = lambda key: "" if d[key][k] is None else d[key][k]
                    rows[(day, place)] = {"date": day, "place": place, "code": g("weather_code"), "precip": g("precipitation_sum"),
                                          "wmax": g("wind_speed_10m_max"), "tmax": g("temperature_2m_max"), "tmin": g("temperature_2m_min")}
        except Exception as e:  # 失敗しても水温の取得は止めない
            print(f"天気の取得失敗: {e}", file=sys.stderr)
    if not rows:
        return
    W_OUT.parent.mkdir(parents=True, exist_ok=True)
    with W_OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=W_HEAD)
        w.writeheader()
        for k in sorted(rows):
            w.writerow(rows[k])
    print(f"天気: {len(rows)} 行(日×地点)")


# 天気の毎時の記録(風・雨)。1年ごとに別ファイル data/weather_hourly_YYYY.csv にためる
WH_HEAD = ["time", "place", "code", "precip", "wind", "dir", "gust"]
WH_VARS = "weather_code,precipitation,wind_speed_10m,wind_direction_10m,wind_gusts_10m"


def collect_weather_hourly():
    """毎時の天気(コード・降水量・風速・風向・瞬間風速)を保存する。
    毎回、直近3日ぶんを取り直して上書きする。初回は過去90日ぶんも取り込む。"""
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    today = now.date()
    now_s = now.strftime("%Y-%m-%dT%H:00")
    rows = {}  # 年 -> {(time, place): row}

    def load(year):
        if year not in rows:
            rows[year] = {}
            f = Path(f"data/weather_hourly_{year}.csv")
            if f.exists():
                with f.open(encoding="utf-8") as fh:
                    for r in csv.DictReader(fh):
                        rows[year][(r["time"], r["place"])] = r
        return rows[year]

    lat = ",".join(str(p[1]) for p in W_PLACES)
    lon = ",".join(str(p[2]) for p in W_PLACES)
    common = f"latitude={lat}&longitude={lon}&hourly={WH_VARS}&timezone=Asia%2FTokyo&wind_speed_unit=ms"
    urls = []
    cur = load(today.year)
    days = {}
    for t, pl in cur:
        days.setdefault(pl, set()).add(t[:10])
    if min((len(days.get(pl[0], ())) for pl in W_PLACES), default=0) < 30:  # 記録の少ない地点があれば過去にさかのぼる
        a, b = today - timedelta(days=90), today - timedelta(days=3)
        urls.append(f"https://archive-api.open-meteo.com/v1/archive?{common}&start_date={a}&end_date={b}")
        load(a.year)
    urls.append(f"https://api.open-meteo.com/v1/forecast?{common}&past_days=2&forecast_days=1")
    for u in urls:
        try:
            data = get_json(u)
            data = data if isinstance(data, list) else [data]
            for (place, _, _), loc in zip(W_PLACES, data):
                h = loc["hourly"]
                for k, t in enumerate(h["time"]):
                    if t > now_s or h["wind_speed_10m"][k] is None:  # 未来や欠測は入れない
                        continue
                    g = lambda key: "" if h[key][k] is None else h[key][k]
                    load(int(t[:4]))[(t, place)] = {"time": t, "place": place, "code": g("weather_code"), "precip": g("precipitation"),
                                                     "wind": g("wind_speed_10m"), "dir": g("wind_direction_10m"), "gust": g("wind_gusts_10m")}
        except Exception as e:
            print(f"毎時の天気の取得失敗: {e}", file=sys.stderr)
    for year, d in rows.items():
        if not d:
            continue
        f = Path(f"data/weather_hourly_{year}.csv")
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=WH_HEAD)
            w.writeheader()
            for k in sorted(d):
                w.writerow(d[k])
    print("毎時の天気:", {y: len(d) for y, d in rows.items()})


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "biwako-fishing-collector/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def station_url(kind, code):
    now = get_json(f"{BASE}/system/tmCrntTime.json")["crntObsTime"]  # "2026/11/06 21:00"
    d, t = now.split(" ")
    return f"{BASE}/files/tmlist/{kind}/{d.replace('/', '')}/{t.replace(':', '')}/{code}.json"


def to_iso(s):  # "2026/11/06 21:00" -> "2026-11-06T21:00"
    return s.replace("/", "-").replace(" ", "T")

# 流入河川の水位(m)。出典: 国土交通省 川の防災情報
# 観測所コードは「事務所コード5けた+種別3けた(004=水位)+観測所番号5けた」。基準水位は川の防災情報の値。
R_OUT = Path("data/rivers.csv")
R_HEAD = ["time", "place", "level"]
R_LV = ("水防団待機水位", "氾濫注意水位", "避難判断水位", "氾濫危険水位")
RIVERS = [  # (表示名, 観測所コード, (水防団待機, 氾濫注意, 避難判断, 氾濫危険)) ※基準が公表されていない川は ()
    ("野洲川 野洲", "2203900400002", (2.5, 3.5, 4.3, 4.8)),
    ("姉川 国友橋", "0640100400056", (1.0, 1.6, 1.8, 2.1)),
    ("安曇川 常安橋", "0640100400081", (1.0, 1.5, 1.9, 2.3)),
    ("愛知川 葉枝見橋", "0640100400041", (2.5, 3.5)),
    ("日野川 仁保橋", "0640100400010", (-0.17, 0.93)),
    ("犬上川 千鳥橋", "0640100400043", (0.8, 1.7, 2.0, 2.3)),
    ("芹川 芹川旭橋", "0640100400046", (0.7, 0.9, 1.0, 1.4)),
    ("宇曽川 金沢大橋", "0640100400102", (1.5, 2.5, 2.9, 3.7)),
    ("天野川 天野川橋", "0640100400054", (0.8, 1.2, 1.4, 1.6)),
    ("石田川 岸脇", "0640100400083", (1.8, 2.1)),
    ("知内川 知内川", "0640100400078", (1.0, 1.3)),
    ("百瀬川 百瀬川", "0640100400079", ()),
    ("和邇川 和邇川", "0640100400002", (1.2, 2.0)),
    ("真野川 真野川", "0640100400003", (0.7, 1.3)),
    ("白鳥川 JR下流橋", "0640100400127", ()),
    ("矢倉川 高根橋", "0640100400130", ()),
    ("大川 岩熊橋", "0640100400069", (0.6, 1.2)),
    ("大浦川 大浦黒山橋", "0640100400070", (2.0, 2.5)),
    ("滝川 JR上流橋", "0640100400115", ()),
    ("葉山川 上鈎池橋", "0640100400008", (0.53, 0.95)),
    ("草津川 西矢倉", "0640100400097", (2.3, 3.1, 3.4, 4.3)),
    ("十禅寺川 南田山", "0640100400119", ()),
]
RIVER_DISP = {"芹川 芹川旭橋": "芹川(滋賀県立大寮横)", "矢倉川 高根橋": "矢倉川(彦根ココス横)", "滝川 JR上流橋": "滝川(北小松)", "草津川 西矢倉": "草津川(帰帆島裏)", "十禅寺川 南田山": "十禅寺川(帰帆島裏)"}
RIVER_ALERTS = {}  # 氾濫注意水位以上の川(通知用): キー -> (名前, 文, 強い通知か)


def collect_rivers():
    RIVER_ALERTS.clear()
    seen = set()
    if R_OUT.exists():
        with R_OUT.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                seen.add((r["time"], r["place"]))
    rows = []
    for label, fcd, th in RIVERS:
        try:
            data = get_json(station_url("stg", fcd))
            for v in data.get("hrValues", []):
                if v.get("stg") is None:
                    continue
                t = to_iso(v["obsTime"])
                if (t, label) in seen:
                    continue
                rows.append({"time": t, "place": label, "level": v["stg"]})
                seen.add((t, label))
            cur = next((v["stg"] for v in data.get("min10Values", []) if v.get("stg") is not None), None)
            if cur is None:
                cur = next((v["stg"] for v in data.get("hrValues", []) if v.get("stg") is not None), None)
            if cur is not None:
                stage = sum(1 for x in th if cur >= x)
                if len(th) >= 2 and stage >= 2:
                    RIVER_ALERTS["r:" + label + ":" + R_LV[stage - 1]] = (
                        RIVER_DISP.get(label, label.split(" ")[0]), f"{R_LV[stage - 1]}を超えています({cur:.2f}m)", stage >= 3)
        except Exception as e:  # 1地点の失敗で全体を止めない
            print(f"河川の取得失敗: {label}: {e}", file=sys.stderr)
    rows.sort(key=lambda r: r["time"])
    new_file = not R_OUT.exists()
    with R_OUT.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=R_HEAD)
        if new_file:
            w.writeheader()
        w.writerows(rows)
    print(f"河川の水位を {len(rows)} 行追記しました(基準超過 {len(RIVER_ALERTS)} 件)")

# 気象庁の警報・注意報(滋賀県)。出典: 気象庁ホームページ
A_OUT = Path("data/alerts.json")
A_NAME = {
    "02": "暴風雪警報", "03": "大雨警報", "04": "洪水警報", "05": "暴風警報", "06": "大雪警報",
    "07": "波浪警報", "08": "高潮警報", "09": "土砂災害警報",
    "10": "大雨注意報", "12": "大雪注意報", "13": "風雪注意報", "14": "雷注意報", "15": "強風注意報",
    "16": "波浪注意報", "17": "融雪注意報", "18": "洪水注意報", "19": "高潮注意報", "20": "濃霧注意報",
    "21": "乾燥注意報", "22": "なだれ注意報", "23": "低温注意報", "24": "霜注意報", "25": "着氷注意報",
    "26": "着雪注意報",
    "32": "暴風雪特別警報", "33": "大雨特別警報", "35": "暴風特別警報", "36": "大雪特別警報",
    "37": "波浪特別警報", "38": "高潮特別警報",
}


def collect_alerts():
    """滋賀県の警報・注意報を data/alerts.json に書き出す(毎回上書き)。
    気象庁は 2026年5月28日に新形式(r8)へ切り替えた。旧URLは更新が止まっているので使わない。"""
    j = get_json("https://www.jma.go.jp/bosai/warning/data/r8/250000.json")
    # 形式の確認用に、気象庁の生データをそのまま保存しておく
    Path("data/alerts_raw.json").write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
    names = {}
    try:
        a = get_json("https://www.jma.go.jp/bosai/common/const/area.json")
        for k in ("class10s", "class15s", "class20s"):
            for code, v in a.get(k, {}).items():
                names[code] = v.get("name", code)
    except Exception as e:
        print(f"地域名の取得に失敗: {e}", file=sys.stderr)
    entries = j if isinstance(j, list) else [j]
    acc, report = {}, ""
    for e in entries:
        if not isinstance(e, dict):
            continue
        report = max(report, str(e.get("reportDatetime", "")))  # 一番新しい発表時刻
        w = e.get("warning")
        if not isinstance(w, dict):
            continue
        for key, items in w.items():
            if not (key.endswith("Items") and isinstance(items, list)):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                ac = str(it.get("areaCode") or it.get("code") or "")
                if not ac:
                    continue
                d = acc.setdefault(ac, {})
                for k in it.get("kinds") or it.get("warnings") or []:
                    code, st = str(k.get("code", "")), str(k.get("status", ""))
                    if not code or st in ("解除", "発表警報・注意報はなし"):
                        continue
                    d[code] = st
    out = {"updated": datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M"),
           "reportTime": report, "areas": []}
    for ac, d in acc.items():
        ws = [{"code": c, "name": A_NAME.get(c, "警報・注意報(コード" + c + ")"), "status": st}
              for c, st in sorted(d.items())]
        out["areas"].append({"code": ac, "name": names.get(ac, ac), "warnings": ws})
    for ar in out["areas"]:
        for w in ar["warnings"]:
            if w["code"] not in A_NAME:
                print(f"未登録のコード: {w['code']} ({ar['name']})", file=sys.stderr)
    A_OUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        if os.environ.get("NTFY_TEST") == "true":
            ntfy_send(topic, "【琵琶湖】テスト通知", "この通知が見えれば設定は成功です。", 3, ["white_check_mark"])
        notify_alerts(out, topic, extra=RIVER_ALERTS)
    print(f"警報・注意報を更新しました({sum(1 for a in out['areas'] if a['warnings'])}地域)")


# ---- スマホへの通知(ntfy)。GitHub の Secrets に NTFY_TOPIC を入れると有効になる ----
APP_URL = "https://suzuhaya5666-lgtm.github.io/Biwakodate/"
N_STATE = Path("data/notified.json")
N_ADV = {"10", "12", "14", "15", "18"}  # 通知する注意報: 大雨・大雪・雷・強風・洪水
N_AREAS = ["大津市", "草津市", "守山市", "野洲市", "近江八幡市", "東近江市", "彦根市", "米原市",
           "長浜市", "高島市", "栗東市", "湖南市", "甲賀市", "日野町", "竜王町", "愛荘町",
           "豊郷町", "甲良町", "多賀町"]


def ntfy_send(topic, title, message, priority=3, tags=None):
    body = {"topic": topic, "title": title, "message": message, "priority": priority,
            "tags": tags or [], "click": APP_URL}
    req = urllib.request.Request("https://ntfy.sh", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "biwako-fishing-collector/0.1"})
    urllib.request.urlopen(req, timeout=30).read()


def notify_alerts(out, topic, send=ntfy_send, extra=None):
    """前回の通知から増えた警報・注意報だけを送る。解除されたものは記録から外れ、再発表時にまた通知される。"""
    lake = lambda nm: any(nm.startswith(x) for x in N_AREAS)  # 大津市南部・大津市北部も含む
    named = any(lake(a["name"]) for a in out["areas"])
    cur = {}
    for a in out["areas"]:
        nm = a["name"]
        if named and not (lake(nm) or nm in ("北部", "南部")):
            continue
        label = "滋賀県" + nm if nm in ("北部", "南部") else nm
        for w in a["warnings"]:
            # 警報・特別警報はすべて通知。注意報は 大雨・大雪・洪水・雷・強風 だけ通知する
            if A_NAME.get(w["code"], "").endswith("注意報") and w["code"] not in N_ADV:
                continue
            cur[label + ":" + w["name"]] = (label, w["name"])
    strong_keys = set()
    for k, (label, name, strong) in (extra or {}).items():
        cur[k] = (label, name)
        if strong:
            strong_keys.add(k)
    old = set()
    if N_STATE.exists():
        try:
            old = set(json.loads(N_STATE.read_text(encoding="utf-8")).get("keys", []))
        except Exception:
            old = set()
    newk = [(k, v) for k, v in cur.items() if k not in old]
    new = [v for _, v in newk]
    if new:
        by = {}
        for label, name in new:
            by.setdefault(label, []).append(name)
        msg = "\n".join(label + ": " + "、".join(ns) for label, ns in by.items())
        has_r = any(k.startswith("r:") for k, _ in newk)
        has_a = any(not k.startswith("r:") for k, _ in newk)
        strong = any("警報" in v[1] for k, v in newk if not k.startswith("r:")) or \
            any(k in strong_keys for k, _ in newk)
        if has_r and not has_a:
            title = "【琵琶湖】川の水位が上がっています"
        elif has_r:
            title = "【琵琶湖】警報・注意報と川の水位"
        else:
            title = "【琵琶湖】" + ("警報" if strong else "注意報") + "が出ました"
        send(topic, title, msg, 5 if strong else 4 if has_r else 3,
             ["warning"] if strong else ["droplet"] if has_r else ["cloud"])
        print(f"通知を送りました({len(new)}件)")
    N_STATE.write_text(json.dumps({"keys": sorted(cur.keys())}, ensure_ascii=False), encoding="utf-8")


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--dump":
        data = get_json(station_url(sys.argv[2], sys.argv[3]))
        print(json.dumps(data.get("obsValue"), ensure_ascii=False, indent=2))
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if OUT.exists():
        with OUT.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                seen.add((r["time"], r["place"], "level" if r["level"] else "flow" if r["flow"] else "temp"))

    rows = []
    for place, kind, code, col, field in STATIONS:
        try:
            data = get_json(station_url(kind, code))
            # 毎時の値(過去2日ぶん)を取り込む。取りこぼしがあっても次回に補われる。
            for v in data.get("hrValues", []):
                if v.get(field) is None:
                    continue
                t = to_iso(v["obsTime"])
                if (t, place, col) in seen:
                    continue
                row = {"time": t, "place": place, "level": "", "flow": "", "temp": ""}
                row[col] = v[field]
                rows.append(row)
                seen.add((t, place, col))
        except Exception as e:  # 1地点の失敗で全体を止めない
            print(f"取得失敗: {place}: {e}", file=sys.stderr)

    rows.sort(key=lambda r: r["time"])
    new_file = not OUT.exists()
    with OUT.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        if new_file:
            w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} 行を追記しました")
    for fn in (collect_weather, collect_weather_hourly, collect_rivers, collect_alerts):
        try:
            fn()
        except Exception as e:
            print(f"天気の記録に失敗({fn.__name__}): {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
