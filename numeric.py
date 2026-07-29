# numeric.py — 数値・日付の抽出と照合（LLMを使わない決定的な処理）
#
# 回答に書かれた数値が、実際に検索結果に存在した値かどうかを機械的に検証するための
# ユーティリティ。graph.py（数値台帳）と reviewers.py（numeric_checker）から使う。
#
# なぜLLMに任せないのか:
# 「83兆ウォン（約9兆円）」を「9兆ウォン（約9兆円）」と書き換えてしまう誤りが実際に
# 発生した。さらに、その結果を別のモデルに検証させたところ、レポートに無い「83兆ウォン」を
# レポートの記述として読み替え、逆に「正確」と判定した。
# 書く側と検証する側の双方で「文脈的にありそうな数値への無意識な正規化」が起きており、
# 数値照合の最終防衛線をLLMに置く設計は成立しない。
# 詳細は docs/accuracy-improvements.md §0.1 を参照。

import re

SCALE = {"兆": 10 ** 12, "億": 10 ** 8, "万": 10 ** 4}

# 単位付きの数値のみを対象にする。単位の無い裸の数値は、日付・件数・IDと
# 区別できず誤検出の温床になるため拾わない。
_UNITS = "ウォン|円|ドル|%|％"

TOKEN_RE = re.compile(
    r"(?P<sign>[-−▲△+])?"
    r"(?P<n1>\d[\d,]*(?:\.\d+)?)(?P<s1>兆|億|万)?"
    r"(?:(?P<n2>\d[\d,]*)(?P<s2>億|万))?"      # 「52兆5763億」のような複合表記
    r"\s*(?P<unit>" + _UNITS + r")"
)

DATE_RE = re.compile(
    r"(?:(?P<year>\d{4})年)?(?:(?P<month>\d{1,2})月)?(?P<day>\d{1,2})日"
)

# 「83兆ウォン（約9兆円）」のような換算の併記
PAIR_RE = re.compile(
    r"(?P<a>\d[\d,]*(?:\.\d+)?(?:兆|億|万)?)\s*(?P<au>ウォン|円|ドル)"
    r"\s*[（(]\s*約?\s*(?P<b>\d[\d,]*(?:\.\d+)?(?:兆|億|万)?)\s*(?P<bu>ウォン|円|ドル)\s*[）)]"
)

# 為替は動くので、桁の取り違えだけを捉えられる広いレンジにする。
# 狭くすると為替変動そのもので誤検出が出て、レビューが信用されなくなる。
CONVERSION_BANDS = {
    ("ウォン", "円"): (0.03, 0.30),
    ("円", "ウォン"): (1 / 0.30, 1 / 0.03),
    ("ドル", "円"): (50.0, 400.0),
    ("円", "ドル"): (1 / 400.0, 1 / 50.0),
}

# 照合時の相対誤差の許容幅。
# 「約84兆ウォン」と「84兆1693億ウォン」（差0.2%）は同じ値の丸めとみなしたいが、
# 「9兆ウォン」と「83兆ウォン」（差89%）は別物として検出する必要がある。
MATCH_TOLERANCE = 0.01

CONTEXT_CHARS = 40


def _to_value(sign, n1, s1, n2, s2) -> float:
    value = float(n1.replace(",", "")) * SCALE.get(s1, 1)
    if n2:
        value += float(n2.replace(",", "")) * SCALE.get(s2, 1)
    return -value if sign in ("-", "−", "▲", "△") else value


def _context(text: str, start: int, end: int) -> str:
    return text[max(0, start - CONTEXT_CHARS):end + CONTEXT_CHARS].replace("\n", " ").strip()


def normalize_unit(unit: str) -> str:
    return "%" if unit in ("%", "％") else unit


def extract_numbers(text: str) -> list[dict]:
    """
    単位付きの数値を抽出する。

    戻り値の各要素:
      {"raw": "83兆ウォン", "value": 8.3e13, "unit": "ウォン", "context": "…"}
    """
    if not text:
        return []
    out = []
    for m in TOKEN_RE.finditer(text):
        out.append({
            "raw": m.group(0).strip(),
            "value": _to_value(m.group("sign"), m.group("n1"), m.group("s1"),
                               m.group("n2"), m.group("s2")),
            "unit": normalize_unit(m.group("unit")),
            "context": _context(text, *m.span()),
        })
    return out


def extract_dates(text: str) -> list[dict]:
    """
    日付表現を文脈つきで抽出する。
    「29日に第2四半期決算の発表を控える」のような予定を台帳へ載せるために使う。
    """
    if not text:
        return []
    out = []
    for m in DATE_RE.finditer(text):
        out.append({
            "raw": m.group(0),
            "context": _context(text, *m.span()),
        })
    return out


def find_conversion_pairs(text: str) -> list[dict]:
    """「A単位1（約B単位2）」形式の換算併記を抽出する"""
    if not text:
        return []
    out = []
    for m in PAIR_RE.finditer(text):
        a = _to_value(None, *_split_scale(m.group("a")), None, None)
        b = _to_value(None, *_split_scale(m.group("b")), None, None)
        out.append({
            "raw": m.group(0),
            "src": a, "src_unit": m.group("au"),
            "dst": b, "dst_unit": m.group("bu"),
            "ratio": (b / a) if a else 0.0,
        })
    return out


def _split_scale(token: str):
    """「83兆」→ ("83", "兆")"""
    m = re.match(r"(\d[\d,]*(?:\.\d+)?)(兆|億|万)?", token)
    return m.group(1), m.group(2)


def conversion_plausible(pair: dict) -> bool:
    """換算の比率が桁として成立しているか"""
    if pair["src"] == 0:
        return True                       # 判定不能。指摘しない
    if pair["src_unit"] == pair["dst_unit"]:
        return abs(pair["src"] - pair["dst"]) < 1e-9
    band = CONVERSION_BANDS.get((pair["src_unit"], pair["dst_unit"]))
    if band is None:
        return True                       # 未知の通貨ペアは判定しない
    return band[0] <= pair["ratio"] <= band[1]


def matches_any(entry: dict, candidates: list[dict]) -> bool:
    """
    entry と同じ値・同じ単位の数値が candidates に存在するか。

    完全一致だけを要求すると「約84兆ウォン」と「84兆1693億ウォン」が別物になり
    誤検出だらけになるため、相対誤差 MATCH_TOLERANCE 以内なら同一とみなす。
    """
    for c in candidates:
        if c["unit"] != entry["unit"]:
            continue
        if entry["value"] == c["value"]:
            return True
        scale = max(abs(entry["value"]), abs(c["value"]))
        if scale and abs(entry["value"] - c["value"]) / scale <= MATCH_TOLERANCE:
            return True
    return False


def collect_from_text(text: str, source: str = "", step: int = 0) -> list[dict]:
    """
    テキストから数値台帳（findings）用のエントリを作る。
    数値と日付の両方を拾う。
    """
    items = []
    for n in extract_numbers(text):
        items.append({"kind": "number", "raw": n["raw"], "context": n["context"],
                      "source": source, "step": step})
    for d in extract_dates(text):
        items.append({"kind": "date", "raw": d["raw"], "context": d["context"],
                      "source": source, "step": step})
    return items


def merge_findings(existing: list[dict], new: list[dict], limit: int = 40) -> list[dict]:
    """
    台帳へ追記する。(raw, contextの先頭20文字) で重複を除き、上限を超えたら古い順に捨てる。
    上限があるのは、履歴と違ってトリミングされない領域なので、
    無制限に伸びるとプロンプトを圧迫するため。
    """
    merged = list(existing)
    seen = {(e["raw"], e.get("context", "")[:20]) for e in merged}
    for item in new:
        key = (item["raw"], item.get("context", "")[:20])
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[-limit:]


def format_findings(findings: list[dict], max_items: int = 40) -> str:
    """台帳をプロンプトへ埋め込む文字列にする"""
    if not findings:
        return ""
    lines = []
    for f in findings[-max_items:]:
        ctx = f.get("context", "")
        if len(ctx) > 60:
            ctx = ctx[:60] + "…"
        src = f.get("source", "")
        lines.append(f"- {f['raw']}（{ctx}）" + (f" ／ {src}" if src else ""))
    return "\n".join(lines)


# 「◯日に発表」等、予定を示す文脈を判定するための語
PENDING_EVENT_WORDS = ("発表", "予定", "控え", "見込み", "公表", "リリース")


def _resolve_date(raw: str, today):
    """
    「29日」「7月29日」「2026年7月29日」を日付へ解決する。
    年・月が省略されている場合は today のものを補う（推測であることは
    呼び出し側で扱う）。解決できなければ None。
    """
    import datetime as _dt

    m = DATE_RE.fullmatch(raw)
    if not m:
        return None
    try:
        return _dt.date(
            int(m.group("year")) if m.group("year") else today.year,
            int(m.group("month")) if m.group("month") else today.month,
            int(m.group("day")),
        )
    except ValueError:
        return None


def pending_event_warnings(findings: list, today=None) -> list[str]:
    """
    台帳の日付のうち、「発表予定」等の文脈を持ち、その日が今日以前に
    到達しているものを警告として返す。

    予想値を確定値のように扱う事故は、予定日が到来していることに
    気づかないまま起きる。実測では、決算発表当日の未明に実行しながら
    予想値をそのまま結論にしていた。LLMの注意力に頼らず、日付の比較は
    コード側で行う。
    """
    import datetime as _dt

    today = today or _dt.date.today()
    warnings = []
    seen = set()
    for f in findings or []:
        if f.get("kind") != "date":
            continue
        context = f.get("context", "")
        if not any(w in context for w in PENDING_EVENT_WORDS):
            continue
        d = _resolve_date(f.get("raw", ""), today)
        if d is None or d > today:
            continue
        key = (f["raw"], context[:20])
        if key in seen:
            continue
        seen.add(key)
        when = "本日" if d == today else f"{d.isoformat()}（すでに経過）"
        warnings.append(f"{f['raw']} は {when} です — 「{context[:50]}」")
    return warnings


# 「情報が得られなかった」と述べていることを検知するための語
ABSENCE_PHRASES = (
    "見つかりません", "見つからず", "見つかりませんでした",
    "確認できません", "確認できませんでした", "得られません", "得られませんでした",
    "限定的", "情報がありません", "データはありません", "不明です",
)


def claims_absence(text: str) -> bool:
    """回答が「情報が得られなかった」と述べているか"""
    return any(p in (text or "") for p in ABSENCE_PHRASES)


def unused_numbers(answer: str, findings: list) -> list[dict]:
    """
    台帳にあるのに回答で使われていない数値を返す。

    「情報が見つかりませんでした」と書きながら、実は取得済みの数値
    （株価の下落率など）を使っていない、という取りこぼしを検出するために使う。
    実測で2回続けて発生している。

    照合は文字列一致ではなく値と単位で行う。回答が「約84兆ウォン」と
    丸めて書いていても、台帳の「84兆1693億ウォン」を使ったとみなす。
    """
    used = extract_numbers(answer or "")
    out = []
    for f in findings or []:
        if f.get("kind") != "number":
            continue
        parsed = extract_numbers(f.get("raw", ""))
        if not parsed:
            continue
        if not matches_any(parsed[0], used):
            out.append(f)
    return out
