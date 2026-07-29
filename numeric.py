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

# 桁の表記。日本語（兆億万）と韓国語（조억만）の両方を受ける。
# エージェントは対象国の言語で検索することがあり、実測では
# 韓国語ソース（NAVER金融、韓国メディア）から得た数値が
# まるごと抽出できておらず、株価の暴落（-14.65%）が台帳に載らなかった。
SCALE = {
    "兆": 10 ** 12, "億": 10 ** 8, "万": 10 ** 4,
    "조": 10 ** 12, "억": 10 ** 8, "만": 10 ** 4,
}

# 単位付きの数値のみを対象にする。単位の無い裸の数値は、日付・件数・IDと
# 区別できず誤検出の温床になるため拾わない。
_UNITS = "ウォン|원|円|ドル|달러|%|％|퍼센트"

# 符号。韓国語の「마이너스」（マイナス）も負号として扱う。
_SIGN = r"[-−▲△+]|마이너스\s*|마이나스\s*"

TOKEN_RE = re.compile(
    r"(?P<sign>" + _SIGN + r")?"
    r"(?P<n1>\d[\d,]*(?:\.\d+)?)(?P<s1>兆|億|万|조|억|만)?"
    r"(?:(?P<n2>\d[\d,]*)(?P<s2>億|万|억|만))?"   # 「52兆5763億」「78조9680억」のような複合表記
    r"\s*(?P<unit>" + _UNITS + r")"
)

DATE_RE = re.compile(
    r"(?:(?P<year>\d{4})年)?(?:(?P<month>\d{1,2})月)?(?P<day>\d{1,2})日"
)

# 「83兆ウォン（約9兆円）」のような換算の併記
PAIR_RE = re.compile(
    r"(?P<a>\d[\d,]*(?:\.\d+)?(?:兆|億|万|조|억|만)?)\s*(?P<au>ウォン|원|円|ドル|달러)"
    r"\s*[（(]\s*約?\s*(?P<b>\d[\d,]*(?:\.\d+)?(?:兆|億|万|조|억|만)?)"
    r"\s*(?P<bu>ウォン|원|円|ドル|달러)\s*[）)]"
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
    if sign and (sign[0] in ("-", "−", "▲", "△") or sign.startswith(("마이너스", "마이나스"))):
        return -value
    return value


def _context(text: str, start: int, end: int) -> str:
    return text[max(0, start - CONTEXT_CHARS):end + CONTEXT_CHARS].replace("\n", " ").strip()


# 表記の違いを吸収する。韓国語ソースの「원」と日本語回答の「ウォン」は
# 同じ通貨なので、同一視しないと照合が成立しない。
_UNIT_ALIASES = {
    "％": "%", "퍼센트": "%",
    "원": "ウォン",
    "달러": "ドル",
}


def normalize_unit(unit: str) -> str:
    return _UNIT_ALIASES.get(unit, unit)


def extract_numbers(text: str) -> list[dict]:
    """
    単位付きの数値を抽出する。

    戻り値の各要素:
      {"raw": "83兆ウォン", "value": 8.3e13, "unit": "ウォン", "context": "…",
       "start": 12, "end": 17}

    start / end は元テキスト上の位置。数値の直後に置かれた [実績] / [予想] の
    タグを読むために使う（tag_after）。
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
            "start": m.start(),
            "end": m.end(),
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
    m = re.match(r"(\d[\d,]*(?:\.\d+)?)(兆|億|万|조|억|만)?", token)
    return m.group(1), m.group(2)


def conversion_plausible(pair: dict) -> bool:
    """換算の比率が桁として成立しているか"""
    if pair["src"] == 0:
        return True                       # 判定不能。指摘しない
    if normalize_unit(pair["src_unit"]) == normalize_unit(pair["dst_unit"]):
        return abs(pair["src"] - pair["dst"]) < 1e-9
    band = CONVERSION_BANDS.get(
        (normalize_unit(pair["src_unit"]), normalize_unit(pair["dst_unit"])))
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


# 台帳がプロンプトに占めてよい文字数の上限。
# 台帳はトリミングされない領域なので、放っておくと際限なく伸びて
# 回答を書くためのトークン枠を食い潰す。実測（コンテキスト8192）では
# プロンプトが7160トークンまで膨らみ、出力枠が1032しか残らなかった。
FINDINGS_CHAR_BUDGET = 1200


def format_findings(findings: list[dict], max_items: int = 40,
                    char_budget: int = FINDINGS_CHAR_BUDGET) -> str:
    """
    台帳をプロンプトへ埋め込む文字列にする。

    新しいものほど関連性が高いので、新しい順に詰めて予算を使い切ったら止める。
    表示は元の順序に戻す。
    """
    if not findings:
        return ""

    picked = []
    used = 0
    for f in reversed(findings[-max_items:]):
        ctx = f.get("context", "")
        if len(ctx) > 45:
            ctx = ctx[:45] + "…"
        src = f.get("source", "")
        line = f"- {f['raw']}（{ctx}）" + (f" ／ {src}" if src else "")
        if used + len(line) > char_budget and picked:
            break
        picked.append(line)
        used += len(line) + 1
    return "\n".join(reversed(picked))


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


# ===== 予想と実績の取り違え、種別タグ、情報量の後退 =====

# 出典側が「これは予想である」と書いているときに現れる語。
# 韓国語ソースも扱うため、전망（見通し）・예상（予想）も入れる。
FORECAST_WORDS = (
    "予想", "見通し", "見込み", "コンセンサス", "予測", "ガイダンス",
    "推定", "計画", "目標", "전망", "예상", "컨센서스",
)

ACTUAL_TAG = "[実績]"
FORECAST_TAG = "[予想]"
UNKNOWN_TAG = "[種別不明]"
_TAGS = (ACTUAL_TAG, FORECAST_TAG, UNKNOWN_TAG)

# 数値に付いた種別タグを見る範囲。「営業利益は9.2兆ウォン [実績]（…）」の
# ように、単位のすぐ後ろに置かれることを想定している。
TAG_LOOKAHEAD = 24


def tag_after(text: str, end: int) -> str:
    """数値の直後にある種別タグを返す。無ければ空文字。"""
    window = (text or "")[end:end + TAG_LOOKAHEAD]
    for tag in _TAGS:
        if tag in window:
            return tag
    return ""


# 文の区切り。小数点（9.35）で切ってしまわないよう、半角ピリオドは
# 前後が数字でない場合だけ区切りとみなす。
_SENTENCE_SPLIT = re.compile(r"(?:[。！？!?\n]+|(?<!\d)[.．](?!\d))")


def sentence_containing(context: str, token: str) -> str:
    """
    文脈のうち、その数値が入っている文だけを返す。

    「-9.35% 하락. 매출 78조9680억원 전망.」のように、1つの文脈に
    実績と予想が同居することがある。文脈全体で「予想」を判定すると、
    隣の文の「전망」に引きずられて実績値を予想と誤判定する。
    """
    if not context:
        return ""
    for part in _SENTENCE_SPLIT.split(context):
        if token and token in part:
            return part
    return context


def _finding_context(entry: dict, findings: list) -> str:
    """回答中の数値に対応する台帳エントリの文脈を返す。"""
    for f in findings or []:
        if f.get("kind") != "number":
            continue
        parsed = extract_numbers(f.get("raw", ""))
        if parsed and matches_any(entry, parsed):
            return f.get("context", "")
    return ""


def forecast_marked_as_actual(answer: str, findings: list) -> list[dict]:
    """
    [実績] と書かれているが、出典側の文脈は予想だった数値を返す。

    実測で、証券会社のコンセンサス（発表前の予想）を実績値として
    レポートに書いた事故が起きている。タグを付ける運用にした以上、
    タグと出典の食い違いは機械的に拾える。
    """
    out = []
    for n in extract_numbers(answer or ""):
        if tag_after(answer, n["end"]) != ACTUAL_TAG:
            continue
        context = _finding_context(n, findings)
        if not context:
            continue
        # 同じ文脈に実績と予想が同居することがあるため、その数値が
        # 入っている文だけを見る
        segment = sentence_containing(context, n["raw"])
        if any(w in segment for w in FORECAST_WORDS):
            out.append({"raw": n["raw"], "context": segment})
    return out


def untagged_ratio(answer: str) -> tuple:
    """(タグの無い数値の数, 単位付き数値の総数) を返す。"""
    numbers = extract_numbers(answer or "")
    if not numbers:
        return (0, 0)
    untagged = [n for n in numbers if not tag_after(answer, n["end"])]
    return (len(untagged), len(numbers))


def dropped_supported_numbers(previous: str, current: str, findings: list) -> list[dict]:
    """
    前回の回答にあり、台帳にも裏付けがあるのに、今回の回答から消えた数値を返す。

    訂正ループは「間違いを消す」方向にしか働かないため、指摘に応じた
    書き直しで実績値ごと落ちることがある。情報量が減る訂正は、
    それ自体を悪化として扱えるようにする。
    """
    if not previous or not current:
        return []
    now = extract_numbers(current)
    out = []
    for n in extract_numbers(previous):
        if matches_any(n, now):
            continue
        if not _finding_context(n, findings):
            continue                      # 台帳に裏付けが無い数値は消えて正しい
        out.append({"raw": n["raw"], "context": n["context"]})
    return out


_SIGNED_RE = re.compile(r"^(?:[-−▲△]|마이너스|マイナス)")
_PLUS_RE = re.compile(r"^\+")


def _sign_of(raw: str) -> int:
    """明示的な符号だけを見る。符号の無い数値は0を返す。"""
    if _SIGNED_RE.match(raw or ""):
        return -1
    if _PLUS_RE.match(raw or ""):
        return 1
    return 0


def sign_conflicts(text: str) -> list[dict]:
    """
    同じ文の中で、変動率と変動額の符号が食い違っている箇所を返す。

    「+5.2%（-1,200円）」のように、率がプラスで額がマイナスというデータは
    どちらかが誤っている。株価サイトのスクレイプでは実際に混入する。

    「前日比 -3.2%、年初来 +12%」のように率同士で符号が違うのは正常なので、
    率（%）と通貨額のペアに限って見る。
    """
    out = []
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        nums = extract_numbers(sentence)
        rates = [n for n in nums if n["unit"] == "%" and _sign_of(n["raw"])]
        amounts = [n for n in nums if n["unit"] != "%" and _sign_of(n["raw"])]
        for r in rates:
            for a in amounts:
                if _sign_of(r["raw"]) != _sign_of(a["raw"]):
                    out.append({"rate": r["raw"], "amount": a["raw"],
                                "context": sentence.strip()[:60]})
    return out
