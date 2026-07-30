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
    "兆": 10 ** 12, "億": 10 ** 8, "万": 10 ** 4, "千": 10 ** 3,
    "조": 10 ** 12, "억": 10 ** 8, "만": 10 ** 4, "천": 10 ** 3,
}

# 単位付きの数値のみを対象にする。単位の無い裸の数値は、日付・件数・IDと
# 区別できず誤検出の温床になるため拾わない。
_UNITS = "ウォン|원|円|ドル|달러|%|％|퍼센트|KRW|JPY|USD"

# 符号。韓国語の「마이너스」（マイナス）も負号として扱う。
_SIGN = r"[-−▲△+]|마이너스\s*|마이나스\s*"

TOKEN_RE = re.compile(
    r"(?P<sign>" + _SIGN + r")?"
    r"(?P<n1>\d[\d,]*(?:\.\d+)?)(?P<s1>兆|億|万|千|조|억|만|천)?"
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
    "원": "ウォン", "KRW": "ウォン",
    "달러": "ドル", "USD": "ドル",
    "JPY": "円",
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


# URL。数値抽出の前に潰す範囲。
#
# 検索結果は「URL: https://…」の行を含んだまま collect_from_text に渡る。
# パーセントエンコーディングの %XX が全部「XX%」として拾われるため、
# 1本のURLで9件のゴミが台帳に入る（実測）。害は3方向に出る。
#   1. merge_findings の上限40件をゴミが食い、本物の数値が押し出される
#   2. 照合の母集団に入るので、回答の「88%」がURLのバイト列と一致して通る
#   3. 置換候補として提案される
# confirmed も台帳から作られるので、ここを塞げば汚染は連鎖的に止まる。
URL_RE = re.compile(r"https?://\S+")


def mask_urls(text: str) -> str:
    """URLを同じ長さの空白に置き換える。

    削除ではなく空白で潰すのは、extract_numbers が返す start / end を
    元テキスト上の位置として保つため。タグの読み取り（tag_after）が
    位置に依存している。

    テキストごと捨てないのは、「URL: …\n概要: 営業利益は9.2兆ウォン」の
    ように1つの文字列にURLと本文が同居しているため。まとめて弾くと
    本物の数値まで落ちる（報告A と同じ事故になる）。
    """
    return URL_RE.sub(lambda m: " " * len(m.group(0)), text or "")


def collect_from_text(text: str, source: str = "", step: int = 0) -> list[dict]:
    """
    テキストから数値台帳（findings）用のエントリを作る。
    数値と日付の両方を拾う。

    URLは数値として拾わない（mask_urls）。呼び出し元4箇所すべてが
    検索結果か取得本文なので、ここで塞げば台帳は一律にきれいになる。
    """
    text = mask_urls(text)
    items = []
    for n in extract_numbers(text):
        # 実績か予想かは収集の時点で機械的に決めておく。書く側の判断に
        # 委ねると混同が繰り返されるため、台帳側を正とする（§7）。
        items.append({"kind": "number", "raw": n["raw"], "context": n["context"],
                      "source": source, "step": step,
                      "value_type": classify_value_type(
                          sentence_containing(n["context"], n["raw"]))})
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
        vtype = f.get("value_type", "")
        tag = f" {VALUE_TYPE_TAGS[vtype]}" if vtype in VALUE_TYPE_TAGS else ""
        line = f"- {f['raw']}{tag}（{ctx}）" + (f" ／ {src}" if src else "")
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


# ===== ラベルと数値の対応、値の種別 =====

# 出典側が「確定した実績」と書いているときに現れる語。
ACTUAL_WORDS = (
    "実績", "確定", "発表した", "計上", "終値", "確報", "速報値", "記録した",
    "だった", "となった", "실적", "확정", "종가",
)

# 通貨コード表記。出典ページが KRW/JPY/USD で書いている場合に拾う。
CURRENCY_CODES = {"KRW": "ウォン", "JPY": "円", "USD": "ドル"}

# ラベルとして採る文字列の最大長。長すぎる前置きはラベルではない。
LABEL_MAX = 12

# ラベルの区切りになる文字（この手前までをラベルとみなす）
_LABEL_STOP = "。．.、,，:：（）()「」『』/／|｜\n\t 　*-–—"
_LABEL_PARTICLES = ("は", "が", "を", "の", "も", "で", "に", "と")


_SCALE_CHARS = "兆億万千조억만천"


def strip_particle(label: str) -> str:
    # 末尾の助詞と、先頭に残った桁の文字（「億売買代金」の「億」）を落とす
    while label and label[-1] in _LABEL_PARTICLES:
        label = label[:-1]
    return label.lstrip(_SCALE_CHARS).strip()


def label_before(text: str, start: int) -> str:
    """数値の直前にあるラベルを返す（「営業利益は9.2兆ウォン」→「営業利益」）。"""
    head = (text or "")[:start]
    out = []
    for ch in reversed(head):
        if ch in _LABEL_STOP or ch.isdigit() or len(out) >= LABEL_MAX:
            break
        out.append(ch)
    return strip_particle("".join(reversed(out)).strip())


def label_after(text: str, end: int) -> str:
    """
    数値の直後にあるラベルを返す。

    株価サイトの表をテキスト化すると「41.74億売買代金0.55%売買回転率」の
    ように、値のうしろにラベルが来る並びになることがある。前だけを見ると
    「売買代金」を1つずれて拾い、誤ったラベルと数値の組ができる。
    """
    tail = (text or "")[end:]
    out = []
    for ch in tail:
        if ch in _LABEL_STOP or ch.isdigit() or len(out) >= LABEL_MAX:
            break
        out.append(ch)
    return strip_particle("".join(out).strip())


# 数値の後ろに来る文末表現はラベルではない
_NON_LABELS = ("です", "でした", "だった", "でしたが", "となった", "になった",
               "であり", "となり", "から", "まで", "ほど", "程度", "以上", "以下")

# 「約650%」の「約」のような数量修飾語はラベルではない。これをラベルとして
# 扱うと、文書中に何度も出てくる「約」が別々の値と結び付き、正しい記述が
# 食い違いと判定される。
_LABEL_MODIFIERS = ("約", "およそ", "ほぼ", "概算", "推定", "最大", "最小", "計", "合計")


def _clean_label(label: str) -> str:
    if not label:
        return ""
    for mod in _LABEL_MODIFIERS:
        if label.endswith(mod):
            label = label[: -len(mod)]
    label = strip_particle(label)
    if not label or label in _LABEL_MODIFIERS:
        return ""
    if label in _NON_LABELS or label.endswith(("です", "でした", "ました", "だった")):
        return ""
    return label


def primary_label(entry: dict) -> str:
    """表示に使うラベル。前にあるものを優先する。"""
    return entry.get("label_before") or entry.get("label_after") or ""


def labeled_values(text: str) -> list[dict]:
    """
    テキストから (ラベル, 値) の組を作る。

    前後どちらにラベルが来る書式もあるため両方を持たせ、照合側で
    「どちらかが一致すれば同じラベルの値」とみなす。
    """
    out = []
    for n in extract_numbers(text or ""):
        out.append({
            "raw": n["raw"],
            "value": n["value"],
            "unit": n["unit"],
            "label_before": _clean_label(label_before(text, n["start"])),
            "label_after": _clean_label(label_after(text, n["end"])),
            "context": n["context"],
        })
    return out


def _labels_of(entry: dict) -> set:
    return {l for l in (entry.get("label_before", ""), entry.get("label_after", "")) if l}


_HANGUL = re.compile(r"[\uac00-\ud7a3]")

# 助詞や空白で区切られた「文章」か、区切りの無い「表のなれの果て」か。
# ラベルを隣接位置から拾えるのは後者だけで、文章に対して同じ判定をすると
# 「28日の終値は前日比-14.65%」の -14.65% を「前日比」以外と呼んだだけで
# 食い違い扱いになってしまう。
_PROSE_MARKERS = ("は", "が", "の", "を", "に", "で", "と", " ", "　", "、", "。")


# 表とみなす条件のしきい値。助詞が無いだけを条件にすると、
# 「純利益9.2兆ウォン」「前年同期比650%増」のような見出し風の短い断片まで
# 表と判定してしまい、隣接順序の判定が自然文へ及ぶ。
# 実際の表のなれの果ては、長く、数字が数珠つなぎに並んでいる。
TABULAR_MIN_CHARS = 25
TABULAR_MIN_DIGIT_RUNS = 3
# 助詞・空白の密度がこれ未満なら「文章ではない」とみなす。ゼロを要求すると、
# 実際の表（「時価総額97,146,675,000千 KRW」のように空白が1つ混じる）が
# 弾かれてしまう。

TABULAR_MAX_PROSE_DENSITY = 0.02

_DIGIT_RUN = re.compile(r"\d[\d,.]*")


def looks_tabular(context: str) -> bool:
    """
    数値の周りが、区切りの無いフィールドの羅列になっているか。

    条件は3つ。ある程度の長さがあること、助詞・空白がほとんど無いこと、
    数字の並びが3つ以上あること。単位の付いた数値ではなく数字の並びで
    数えるのは、株数や売買代金のように単位が離れている欄があるため。
    """
    text = (context or "")
    if len(text) < TABULAR_MIN_CHARS:
        return False
    markers = sum(text.count(m) for m in _PROSE_MARKERS)
    if markers / len(text) >= TABULAR_MAX_PROSE_DENSITY:
        return False
    return len(_DIGIT_RUN.findall(text)) >= TABULAR_MIN_DIGIT_RUNS


def _comparable_labels(a: str, b: str) -> bool:
    """
    ラベル同士を突き合わせてよいか。

    出典が韓国語（영업이익）で回答が日本語（営業利益）というのは通常の
    運用であり、文字種が違うだけで「食い違い」と判定してはいけない。
    """
    if not a or not b:
        return False
    return bool(_HANGUL.search(a)) == bool(_HANGUL.search(b))


def _labels_conflict(answer_labels: set, source_labels: set) -> bool:
    """回答のラベルと出典のラベルが、比較可能なうえで一致しないか。"""
    comparable = [(a, s) for a in answer_labels for s in source_labels
                  if _comparable_labels(a, s)]
    if not comparable:
        return False
    return not any(a == s or a in s or s in a for a, s in comparable)


# 「予想と比べてどうだったか」を語る言い回し。「予想」の語は入っているが、
# 文が語っている数値そのものは実績側にある。
#
# 実データ（tests/fixtures/raw/doc28_headlines_actual.txt）では、実績を報じる
# 13件のうち7件がこの形で forecast に誤分類されていた。「予想を下回った」の
# 主語は実績であって、予想値ではない。
COMPARISON_WORDS = (
    "予想を下回", "予想を上回", "予想に届か", "予想下回り", "予想上回り",
    "予想下振れ", "予想上振れ", "予想に反し", "予想を嫌気",
    "コンセンサスを下回", "コンセンサスを上回",
    "実際の",
)

# その数値自身が予想であると宣言している言い回し。比較語より強い。
# 「市場予想を上回る見通しだ」は、比較語と宣言語が同居していても予想の話。
DECLARES_FORECAST = (
    "見通し", "見込ま", "見込み", "と予想", "予想される", "予想する",
    "予想によると", "コンセンサス予想", "予測",
)


def classify_value_type(context: str) -> str:
    """
    出典の文脈から、その数値が実績か予想かを機械的に判定する。

    戻り値は "actual" / "forecast" / "unknown"。
    フリーテキストの [実績] タグに頼らず、収集の時点で型を付けるための関数。

    判定は3段構え。

    1. 宣言語（「見通し」「と予想」）があれば forecast。比較語と同居しても
       こちらを採る。数値自身が予想だと言っている方が強い証拠だから。
    2. 比較語（「予想を下回った」）しか無ければ actual。予想の語が入って
       いるだけで、語られている数値は実績。
    3. どちらでもなければ、従来どおり FORECAST_WORDS → ACTUAL_WORDS の順で
       見て、決め手が無ければ unknown。

    2 を足す前は「予想」が1文字でも入れば forecast だったため、決算の実績を
    報じる見出しがほぼ全滅していた。逆に unknown を減らそうとして「文脈的に
    実績だろう」まで actual に寄せることはしない。誤って actual と付けた値は
    そのまま断定として回答に出てしまうので、決め手が無いときは unknown を
    残す方が安全側になる。
    """
    text = context or ""
    if any(w in text for w in DECLARES_FORECAST):
        return "forecast"
    if any(w in text for w in COMPARISON_WORDS):
        return "actual"
    if any(w in text for w in FORECAST_WORDS):
        return "forecast"
    if any(w in text for w in ACTUAL_WORDS):
        return "actual"
    return "unknown"


VALUE_TYPE_TAGS = {"actual": ACTUAL_TAG, "forecast": FORECAST_TAG, "unknown": UNKNOWN_TAG}


_WS = re.compile(r"[\s　]+")


def _normalize_digits(text: str) -> str:
    return _WS.sub("", (text or "").replace(",", "").replace("，", ""))


def appears_verbatim(raw: str, source_text) -> bool:
    """
    数値の表記が、出典の原文にそのまま出てくるか。

    抽出は完璧ではない（改行が数値の途中に入る、空白が挟まる、単位が
    離れる等）。抽出結果だけで「出典に存在しない」と断じると、原文には
    確かに書いてある値を捏造として却下してしまう。原文の文字列でも確認する。
    """
    if not raw or not source_text:
        return False
    norm_raw = _normalize_digits(raw)
    return any(norm_raw in _normalize_digits(t) for t in _as_texts(source_text))


def _as_texts(source_text) -> list:
    """文字列でもリストでも受け、重複を除いたテキストの並びにする。"""
    if not source_text:
        return []
    texts = [source_text] if isinstance(source_text, str) else list(source_text)
    seen, out = set(), []
    for t in texts:
        t = t or ""
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def cooccurs(label: str, raw: str, source_text, window: int = 12) -> bool:
    """
    出典の原文で、そのラベルとその値が近くに出てくるか。

    同じラベル（「売上高」）が文書内に何度も出てきて、それぞれ別の値と
    組になっているのが普通である。「そのラベルの代表値はこれ1つ」と
    決め打ちすると、正しい組み合わせを食い違いと判定してしまう。
    出現ごとに独立して見る。

    窓を狭く（12文字）取るのは、「その出現のすぐ隣にラベルがある」ことだけを
    確かめたいため。広く取ると、同じ段落に別の指標のラベルがあるだけで
    何でも通ってしまい、付け替えの検出が効かなくなる。
    """
    if not label or not raw:
        return False
    norm_raw = _normalize_digits(raw)
    norm_label = _normalize_digits(label)
    # テキストは1件ずつ見る。連結した文字列で探すと、別のページの末尾と
    # 次のページの先頭がたまたま隣り合って「近くにある」と誤判定する
    for text in _as_texts(source_text):
        norm_src = _normalize_digits(text)
        start = 0
        while True:
            idx = norm_src.find(norm_raw, start)
            if idx < 0:
                break
            left = max(0, idx - window)
            if norm_label in norm_src[left:idx + len(norm_raw) + window]:
                return True
            start = idx + 1
    return False


def label_value_mismatches(answer: str, findings: list, source_text="") -> list[dict]:
    """
    回答の「ラベル: 値」の組が、出典側の組と食い違っているものを返す。

    値そのものは出典のどこかに存在するため、値の照合だけではすり抜ける。
    実測で、区切りの無い株価データ（「41.74億売買代金0.55%売買回転率」）から
    ラベルを1つずらして拾った組が、そのまま検証を通過している。

    source_text には検索結果の原文を渡す。「回答のラベルが出典に存在するのに
    別の値へ付いている」ときだけ付け替えとみなすため。出典に無いラベル
    （出典の「終値」を回答が「株価」と書く等）は、言い換えであって誤りではない。
    """
    source = []
    for f in findings or []:
        if f.get("kind") != "number":
            continue
        for lv in labeled_values(f.get("context", "")):
            source.append(lv)
    if not source:
        return []

    out = []
    source_blob = "\n".join(_as_texts(source_text))

    for a in labeled_values(answer or ""):
        labels = _labels_of(a)
        if not labels:
            continue

        same_value = [s for s in source
                      if s["unit"] == a["unit"] and matches_any(a, [s])]
        tabular_hits = [s for s in same_value
                        if looks_tabular(sentence_containing(s["context"], s["raw"]))]

        # (1) 表のなれの果て: 隣接する順序で判定する。
        # 「…売買代金0.55%売買回転率配当利回り--…」のように、正しいラベルの
        # すぐ後ろに別のラベルが続く。距離ではなく「値のすぐ隣か」を見る。
        if tabular_hits:
            if any(_adjacent_label(l, s) for s in tabular_hits for l in labels):
                continue
            if not any(l in source_blob for l in labels):
                continue                   # 出典に無いラベル＝言い換え
            hit = tabular_hits[0]
            out.append({
                "label": primary_label(a),
                "raw": a["raw"],
                "expected": sorted(_labels_of(hit))[:3],
                "reason": "label",
                "context": hit["context"],
            })
            continue

        # (2) 文章: 原文でそのラベルと値が隣り合っていれば正しい組み合わせ。
        # 同じラベルが他の値にも付いていても関係ない（1ラベル・複数値）
        if any(cooccurs(l, a["raw"], source_text) for l in labels):
            continue

        # (3) 同じラベルが出典にあるのに、値が違う
        same_label = [s for s in source if labels & _labels_of(s)]
        if same_label:
            if any(s["unit"] == a["unit"] and matches_any(a, [s]) for s in same_label):
                continue
            out.append({
                "label": primary_label(a),
                "raw": a["raw"],
                "expected": [s["raw"] for s in same_label][:3],
                "reason": "value",
                "context": a["context"],
            })
    return out


def _adjacent_label(answer_label: str, entry: dict) -> bool:
    """
    表形式のデータで、そのラベルが値のすぐ隣に来ているか。

    区切りが無いテキストではラベルが数珠つなぎになる（「売買回転率配当利回り」）。
    値の直前なら末尾、直後なら先頭に来ているものが、その値のラベルである。
    """
    before = entry.get("label_before", "")
    after = entry.get("label_after", "")
    if not answer_label:
        return False
    if before and (before.endswith(answer_label) or answer_label.endswith(before)):
        return True
    if after and (after.startswith(answer_label) or answer_label.startswith(after)):
        return True
    return False


# 置換候補として許す桁の開き。営業利益（兆ウォン）の候補に
# 株価（数十万ウォン）を出さないための帯。
CANDIDATE_MAGNITUDE_RATIO = 1000


def candidates_for(entry: dict, findings: list, limit: int = 3,
                   label: str = "") -> list[str]:
    """
    置き換え候補になりうる値を台帳から探す。

    条件は3つ。単位が同じ、桁が近い（1000倍以内）、ラベルが分かるなら一致。
    単位だけで絞ると、兆ウォンの利益の候補に株価や指数が並ぶ。
    実測で、無関係な値が候補として提示されている。
    """
    same_label, same_scale = [], []
    for f in reversed(findings or []):
        if f.get("kind") != "number":
            continue
        parsed = extract_numbers(f.get("raw", ""))
        if not parsed or parsed[0]["unit"] != entry.get("unit"):
            continue
        value = abs(parsed[0]["value"])
        base = abs(entry.get("value", 0) or 0)
        if base and value:
            ratio = max(value / base, base / value)
            if ratio > CANDIDATE_MAGNITUDE_RATIO:
                continue                   # 桁が違いすぎる。別物である
        if f["raw"] in same_label or f["raw"] in same_scale:
            continue
        f_labels = {l for lv in labeled_values(f.get("context", ""))
                    if _normalize_digits(lv["raw"]) == _normalize_digits(f["raw"])
                    for l in _labels_of(lv)}
        if label and any(label == l or label in l or l in label for l in f_labels):
            same_label.append(f["raw"])
        else:
            same_scale.append(f["raw"])
    return (same_label + same_scale)[:limit]


def candidate_suggestion(entry: dict, findings: list, label: str = "",
                         limit: int = 3) -> tuple:
    """
    置換候補を、確度つきで返す。戻り値は (種別, 値のリスト)。

      "labeled"   同じ項目名の値が見つかった。そのまま置き換えられる
      "unlabeled" 項目名が取れないので、単位と桁だけで拾った参考値
      "none"      候補なし

    項目名が分かっているのに同じ項目名の値が無い場合、単位と桁が近いだけの
    値を候補として並べない。実測（doc26）で、営業利益の置換候補に
    「世界のメモリ市場規模1,500兆ウォン」が並んでいた。桁の帯だけでは、
    別の対象の値を弾けない。
    """
    if label:
        labeled = candidates_for(entry, findings, limit=limit, label=label)
        matched = [c for c in labeled
                   if any(label == l or label in l or l in label
                          for lv in labeled_values(_context_of(c, findings))
                          if _normalize_digits(lv["raw"]) == _normalize_digits(c)
                          for l in _labels_of(lv))]
        return ("labeled", matched[:limit]) if matched else ("none", [])
    hints = candidates_for(entry, findings, limit=limit)
    return ("unlabeled", hints) if hints else ("none", [])


def _context_of(raw: str, findings: list) -> str:
    for f in findings or []:
        if f.get("raw") == raw:
            return f.get("context", "")
    return ""


def tag_conflicts(answer: str, findings: list) -> list[dict]:
    """
    回答の [実績]/[予想] タグが、台帳に記録した value_type と食い違うものを返す。

    タグをフリーテキストの判断に委ねると混同が繰り返される。収集時点で
    機械的に付けた型（classify_value_type）を正とし、書かれたタグと突き合わせる。
    """
    out = []
    for n in extract_numbers(answer or ""):
        written = tag_after(answer, n["end"])
        if not written or written == UNKNOWN_TAG:
            continue
        for f in findings or []:
            if f.get("kind") != "number":
                continue
            parsed = extract_numbers(f.get("raw", ""))
            if not parsed or not matches_any(n, parsed):
                continue
            vtype = f.get("value_type") or classify_value_type(
                sentence_containing(f.get("context", ""), f.get("raw", "")))
            if vtype == "unknown":
                break
            expected = VALUE_TYPE_TAGS[vtype]
            if expected != written:
                out.append({"raw": n["raw"], "written": written,
                            "expected": expected, "context": f.get("context", "")})
            break
    return out


UNVERIFIED_MARK = "（出典未確認）"

# 本文が文字数上限で切れたことを示す印。取得側が付け、検証側が読む。
# 切れた先にある数値を書いた場合と、何も無いところから作った場合とでは
# 意味が違うので、区別できるようにしておく。
TRUNCATION_MARK = "…（本文はここで切れています）"


# ===== 履歴エントリの出所 =====
#
# 履歴の role="result" は、もともと2種類のものが同じ名前で混ざっていた。
#   1. エージェントが外から取ってきたもの（検索結果・取得した本文）
#   2. 機械が内部で書いたもの（correct / critic の差し戻し文、各種ガード）
#
# 照合側はこれを一律に「出典」として扱っていたため、correct が却下した
# 捏造値が、その指摘文ごと出典に化けて次のラウンドで通っていた。
# 1回却下した値が2回目に素通りするので、訂正の往復が実質1回で頭打ちになる。
#
# 文言（「（自動訂正チェック）」で始まるか等）で見分ける手もあるが、
# 文言を変えた瞬間に静かに壊れる。積む側に出所を書かせる。
ORIGIN_SOURCE = "source"        # 検索結果・取得した本文
ORIGIN_COMPUTED = "computed"    # サンドボックスで実行したコードの出力
ORIGIN_INTERNAL = "internal"    # 機械が書いたメッセージ

# 照合の母集団に入れてよい出所。
#
# computed を入れているのは、計算で出した値を「出典に無い」と却下すると
# 正しい数値を落とす事故（報告A と同じ形）になるため。外から取ってきた
# ものではないので名前は分けてあり、締めたくなったらこの集合から外すだけでよい。
SOURCE_ORIGINS = (ORIGIN_SOURCE, ORIGIN_COMPUTED)

# 印の無いエントリの扱い。source に倒す（fail-open）。
#
# internal に倒すと、付け忘れが1箇所でもあった時点で実在する数値が
# 「出典に無い」と却下される。静かに起きるうえ、報告A と同じ壊れ方になる。
# source に倒せば、付け忘れた箇所は従来どおりに動くだけで、新しい事故は出ない。
# 付け忘れ自体は tests 側の構造チェックで落とす。
DEFAULT_ORIGIN = ORIGIN_SOURCE

# origin を付ける前の履歴で、機械が書いたメッセージを見分けるための書き出し。
# 新しい履歴では使わない（文言を変えると静かに壊れるため）。
LEGACY_INTERNAL_MARKS = ("（自動訂正チェック）", "複数のレビュアーから", "（自動チェック）",
                         "（自動品質チェック）")


def entry_origin(entry: dict) -> str:
    return (entry or {}).get("origin") or DEFAULT_ORIGIN


def is_source_entry(entry: dict) -> bool:
    """照合の母集団に入れてよい履歴エントリか。"""
    if (entry or {}).get("role") != "result":
        return False
    return entry_origin(entry) in SOURCE_ORIGINS


def source_texts(history: list) -> list[str]:
    """履歴のうち、出典として扱ってよい本文だけを並びで返す。

    連結せずリストで返すのは、連結すると別々のページの末尾と先頭が
    たまたま隣り合って「ラベルが近い」と誤判定するため。
    """
    return [e.get("content", "") for e in (history or []) if is_source_entry(e)]


def has_origin_marks(history: list) -> bool:
    """この履歴が origin を持つ世代のものか。"""
    return any(e.get("origin") for e in (history or []))


def internal_texts(history: list) -> list[str]:
    """機械が書いたメッセージだけを並びで返す（差し戻し文の拾い直し用）。

    出所の既定値を source に倒してある（DEFAULT_ORIGIN）ため、この向きの
    絞り込みでは印の無いエントリが1件も引っかからない。印を付ける前の
    履歴を読むと差し戻し文が消えるので、その世代だけ従来の文言判定に落とす。
    印が1つでもあれば origin だけを信じる（混在した履歴で二重に拾わない）。
    """
    if not has_origin_marks(history):
        return [e.get("content", "") for e in (history or [])
                if e.get("role") == "result"
                and (e.get("content", "") or "").startswith(LEGACY_INTERNAL_MARKS)]
    return [e.get("content", "") for e in (history or [])
            if e.get("role") == "result" and entry_origin(e) == ORIGIN_INTERNAL]


def mechanical_fixes(answer: str, findings: list, history: list = None) -> tuple:
    """
    差し戻す予算が無いときに、機械だけで確実に直せる分を適用する。

    やることは2つだけ。判断が要る書き換えはしない。
      1. 種別タグが台帳と食い違っているものを、台帳の型に合わせる
      2. 出典に見当たらない数値の直後に「（出典未確認）」を付ける

    戻り値は (直した本文, 適用した内容のリスト)。

    判定はURLを潰したテキストに対して行い、返す本文は元のテキストのまま。
    回答には「出典: https://…」が入るので、潰さないとURLのバイト列に
    「（出典未確認）」を付けてしまう。空白で潰しているので位置は一致する
    （mask_urls は長さを変えない）。
    """
    text = answer or ""
    applied = []

    # タグの是正は正規表現で内容置換するので、判定だけマスク済みで行えばよい
    for t in tag_conflicts(mask_urls(text), findings or []):
        pattern = re.escape(t["raw"]) + r"(\s*)" + re.escape(t["written"])
        new_text, count = re.subn(pattern, t["raw"] + r"\1" + t["expected"], text, count=1)
        if count:
            text = new_text
            applied.append(
                f"「{t['raw']}」の種別を {t['written']} から {t['expected']} に直しました"
                f"（出典の文脈に基づく）"
            )

    # 「（出典未確認）」を付けるかどうかの判定。ここも出典由来だけを見る。
    # 差し戻し文を混ぜると、却下したばかりの値に印が付かなくなる。
    known = []
    for src in source_texts(history):
        known.extend(extract_numbers(src))
    for f in findings or []:
        if f.get("kind") == "number":
            known.extend(extract_numbers(f.get("raw", "")))

    # 位置がずれないよう、後ろから挿入する。
    # 数値の列挙はマスク済みテキストから行い、挿入は元テキストに対して行う。
    # マスクは長さを変えないので end の位置は両者で一致する。
    # タグ是正で長さが変わっている可能性があるので、ここで取り直す。
    for n in sorted(extract_numbers(mask_urls(text)), key=lambda x: x["end"], reverse=True):
        if matches_any(n, known):
            continue
        if text[n["end"]:n["end"] + len(UNVERIFIED_MARK)] == UNVERIFIED_MARK:
            continue
        text = text[:n["end"]] + UNVERIFIED_MARK + text[n["end"]:]
        applied.append(f"「{n['raw']}」に{UNVERIFIED_MARK}を付けました（出典に見当たらないため）")

    return text, applied


def confirmed_from(answer: str, findings: list) -> list[dict]:
    """
    出典と一致した数値を「確定済みフィールド」として抜き出す。

    一度検証を通った値が、次の書き直しで理由なく差し替わる後退を
    防ぐために使う（実測で、実績値が消えて予想値に入れ替わっている）。
    """
    out = []
    for lv in labeled_values(mask_urls(answer)):
        for f in findings or []:
            if f.get("kind") != "number":
                continue
            parsed = extract_numbers(f.get("raw", ""))
            if parsed and matches_any(lv, parsed):
                out.append({
                    "raw": lv["raw"],
                    "label": primary_label(lv),
                    "value_type": f.get("value_type", "unknown"),
                })
                break
    return out


def merge_confirmed(existing: list, new: list, limit: int = 20) -> list[dict]:
    out = list(existing or [])
    seen = {(c["raw"], c.get("label", "")) for c in out}
    for c in new or []:
        key = (c["raw"], c.get("label", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out[-limit:]


def format_confirmed(confirmed: list, char_budget: int = 400) -> str:
    """確定済みフィールドをプロンプトへ埋め込む。"""
    lines, used = [], 0
    for c in reversed(confirmed or []):
        tag = VALUE_TYPE_TAGS.get(c.get("value_type", ""), "")
        line = f"- {c.get('label') or '（ラベルなし）'}: {c['raw']} {tag}".rstrip()
        if used + len(line) > char_budget and lines:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(reversed(lines))


def missing_confirmed(answer: str, confirmed: list) -> list[dict]:
    """確定済みなのに、今回の回答から消えた値を返す。

    URLは潰す。潰さないと、出典URLに偶然含まれる数値が確定済みの値と
    一致して「まだ本文にある」と誤判定し、補記が抑止される。
    """
    now = extract_numbers(mask_urls(answer))
    out = []
    for c in confirmed or []:
        parsed = extract_numbers(c.get("raw", ""))
        if not parsed:
            continue
        if not matches_any(parsed[0], now):
            out.append(c)
    return out


def append_missing_confirmed(answer: str, missing: list) -> tuple:
    """
    確定済みなのに本文から落ちた値を、末尾に一覧として戻す。

    本文の文脈へ差し込むのは機械にはできない（どの文のどこに入れるべきかが
    決まらない）ので、追記にとどめる。検出だけして消えたままにするよりは、
    読み手に見える形で戻す方がよい。
    """
    if not missing:
        return answer, []
    lines = []
    for c in missing:
        tag = VALUE_TYPE_TAGS.get(c.get("value_type", ""), "")
        label = c.get("label") or "（ラベルなし）"
        lines.append(f"- {label}: {c['raw']} {tag}".rstrip())
    block = (
        "\n\n**検証済みだが本文に反映されなかった値**\n\n"
        + "\n".join(lines)
        + "\n\n_これらは出典と一致することを確認済みの数値です。"
        "書き直しの過程で本文から抜けたため、機械的に補記しました。_"
    )
    applied = [f"確定済みの「{c['raw']}」を末尾に補記しました" for c in missing]
    return answer + block, applied


# ===== 株価の時系列テーブル（単位も区切りも無い） =====
#
# HTML→テキスト変換で区切りが消えた時系列表は、こういう1行になる。
#
#   2026/7/28135.91136.49128.29130.1751,088,282130.17
#
# 単位が付かないので TOKEN_RE では1件も拾えず、回答が正しく引用した
# 「130.17ドル」が「出典に見当たらない」と却下された（実測 2026-07-30）。
#
# ここは汎用の数値抽出には手を入れず、専用の経路として切り出す。
# 成立するのは「価格は小数2桁」という構造的な前提があるからで、
# 前提の無い場所へこの解釈を広げてはいけない。

# 日付。2026/7/28 と 2026-07-28 を受ける
_OHLC_DATE = r"\d{4}[/-]\d{1,2}[/-]\d{1,2}"

# 価格トークン。カンマ区切りの整数部＋小数2桁、または7桁までの整数＋小数2桁。
# 出来高（51,088,282）のような桁数の大きい値は、後ろに別の値が連結されて
# いることが多く信用できないため、名前の付いた価格欄だけを採用する。
_OHLC_PRICE = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}|\d{1,7}\.\d{2}")

_OHLC_ROW = re.compile(r"(?P<date>" + _OHLC_DATE + r")(?P<rest>[\d.,]{8,})")

# ヘッダ行に現れる欄名。並び順はサイトごとに違うので、必ずヘッダから読む。
OHLC_FIELDS = ("始値", "高値", "安値", "終値", "調整後終値",
               "出来高", "売買高", "前日比", "変化率")
PRICE_FIELDS = ("始値", "高値", "安値", "終値", "調整後終値")


def parse_ohlc_header(text: str) -> list:
    """
    ヘッダ行から欄の並びを読む。「日付始値高値安値終値出来高調整後終値」→ 並び。

    並び順はサイトごとに違う（Yahoo!は始値から、Investing.comは終値から）。
    ヘッダが無ければ空リストを返し、その場合は欄名を付けない。
    """
    for line in (text or "").splitlines():
        if "日付" not in line or len(line) > 60:
            continue
        order, pos = [], line.index("日付") + 2
        rest = line[pos:]
        while rest:
            for field in OHLC_FIELDS:
                if rest.startswith(field):
                    order.append(field)
                    rest = rest[len(field):]
                    break
            else:
                break
        if len(order) >= 3:
            return order
    return []


def parse_ohlc_rows(text: str) -> list[dict]:
    """
    時系列表の行を {"date", "fields": {欄名: 値}, "values": [値]} に分解する。

    欄名が分からない場合（ヘッダなし）は fields を空にして values だけ返す。
    値の対応を推測で埋めると、ラベルの付け替えと同じ事故になる。
    """
    order = parse_ohlc_header(text)
    rows = []
    for m in _OHLC_ROW.finditer(text or ""):
        values = _OHLC_PRICE.findall(m.group("rest"))
        if not values:
            continue
        fields = {}
        for name, value in zip(order, values):
            if name in PRICE_FIELDS:
                fields[name] = value
        rows.append({"date": m.group("date"), "values": values, "fields": fields})
    return rows


def ohlc_values(source_text) -> list[float]:
    """
    時系列表から読み取れた価格の値を返す（照合専用）。

    単位が無いので「値が出典に存在するか」の判定にだけ使う。
    台帳へは入れない。1ページで数十件になり、台帳の上限（40件）と
    プロンプト予算を株価表だけで埋めてしまうため。
    """
    out = []
    for text in _as_texts(source_text):
        for row in parse_ohlc_rows(text):
            names = row["fields"]
            picked = list(names.values()) if names else row["values"][:4]
            for raw in picked:
                try:
                    out.append(float(raw.replace(",", "")))
                except ValueError:
                    continue
    return out


# 時系列表の値は丸めのない実数なので、照合は完全一致で行う。
# MATCH_TOLERANCE（1%）は「約84兆ウォン」のような丸め表記のための幅であり、
# 株価に当てると隣の日の終値（128.29 と 127.29 は差0.78%）まで通ってしまう。
BARE_MATCH_TOLERANCE = 0.0


def matches_bare(entry: dict, values: list,
                 tolerance: float = BARE_MATCH_TOLERANCE) -> bool:
    """単位を問わず、同じ値が時系列表にあるか（完全一致）。"""
    target = abs(entry.get("value", 0) or 0)
    if not target:
        return False
    for v in values:
        if v == target:
            return True
        scale = max(abs(v), target)
        if tolerance and scale and abs(v - target) / scale <= tolerance:
            return True
    return False
