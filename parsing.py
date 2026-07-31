# parsing.py — LLM出力のパース。ここが唯一の定義箇所。
#
# なぜ切り出したか
# ----------------
# DONE本文の取り出しが3系統7箇所に散っていた。行頭限定の厳密版
# （graph._parse_done / executor._extract_done_text）と、素朴な
# `split("DONE:")` 版（graph._extract_done / _extract_previous_done /
# critic_step 内インライン / correct_step の書き戻し / run.py）である。
#
# 素朴版は、モデルが**書式そのものに言及した文**を書くと壊れる。実測:
#
#     THOUGHT: 最終回答を「DONE: 」形式で再構成します
#     DONE: 売上高は12兆5000億ウォンでした。
#
# ここで `split("DONE:")[1]` は「」形式で再構成します」を返す。
# 厳密版は正しい本文を返す。両者が混在していたため、
# **数値検証は「」形式で再構成します」に対して走り、利用者には正しい本文が
# 返る**という状態になっていた。検証ログ上は「問題なし」と出るので、
# 外から気づけない。
#
# したがって「どちらでも大差ない選択」ではない。行頭限定に一本化する。
#
# 読みだけでなく書き戻しもここに置く。correct_step は
# `content.split("DONE:")[0]` を head として本文を差し替えていたが、
# 上の例では head が "THOUGHT: 最終回答を「" になり、再構成後の
# `DONE:` が行頭に来なくなる。すると厳密版が本文を拾えず、最終回答が
# 空になる。読み側だけ直すとこれが発火するので、対で置く。

import re

# 行頭（前置きの空白・全角空白のみ許す）の DONE:
DONE_LINE_RE = re.compile(r"^[ \t　]*DONE:[ \t　]*", re.MULTILINE)

# `ACTION: DONE` を単独行で書く形式。実測で頻出するため受け付ける
# （弾くと1ループ丸ごと無駄になり、再試行でも同じ形式が出てくる）。
ACTION_DONE_RE = re.compile(r"^[ \t　]*ACTION:[ \t　]*DONE[ \t　]*$", re.MULTILINE)

_LEADING_THOUGHT_RE = re.compile(r"^THOUGHT:[ \t　]*")


def parse_done(text: str) -> str:
    """
    1つのメッセージ本文から DONE の本文を取り出す。無ければ空文字。

    行頭の `DONE:` を優先し、無ければ `ACTION: DONE` 形式を見る。
    """
    text = text or ""

    m = DONE_LINE_RE.search(text)
    if m:
        content = text[m.end():].strip()
        if content:
            return content

    m = ACTION_DONE_RE.search(text)
    if m:
        rest = _LEADING_THOUGHT_RE.sub("", text[m.end():].strip())
        if rest:
            return rest

    return ""


def find_done_entry(history: list) -> tuple[int, str]:
    """
    履歴の末尾側から、DONE本文を持つ assistant エントリを1件探す。

    戻り値は (インデックス, 本文)。見つからなければ (-1, "")。

    本文が取れないエントリは飛ばして、さらに前を見る。
    executor._extract_done_text が以前からそう振る舞っており、
    「DONE: と書いてあるのに本文が空」のときに空文字で確定させるより、
    直前の版を返したほうが実害が小さい。
    """
    for i in range(len(history or []) - 1, -1, -1):
        entry = history[i]
        if entry.get("role") != "assistant":
            continue
        body = parse_done(entry.get("content", ""))
        if body:
            return i, body
    return -1, ""


def extract_done(history: list) -> str:
    """最新の DONE 本文。無ければ空文字。"""
    return find_done_entry(history)[1]


def extract_previous_done(history: list) -> str:
    """
    1つ前の版の DONE 本文を返す。

    訂正のたびに新しいDONEが積まれるので、直前の版と比べれば
    「訂正の結果、正しい数値まで落ちた」ことを検出できる。

    素朴版は `"DONE:" in content` でエントリを数えていたため、
    本文中で DONE: に言及しただけのメッセージも1版として数えていた。
    ここでは本文が取れたものだけを版として数える。
    """
    seen = 0
    for entry in reversed(history or []):
        if entry.get("role") != "assistant":
            continue
        body = parse_done(entry.get("content", ""))
        if not body:
            continue
        seen += 1
        if seen == 2:
            return body
    return ""


def replace_done_body(content: str, new_body: str) -> str:
    """
    1つのメッセージの DONE 本文だけを差し替える。前置き（THOUGHT 等）は残す。

    切り出し位置は**行頭マッチの開始位置**にする。`split("DONE:")[0]` だと、
    本文より前に "DONE:" という文字列が出てくる場合に切りすぎて、
    差し替え後の `DONE:` が行頭に来なくなる（＝以後どのパーサも本文を
    拾えなくなる）。
    """
    content = content or ""

    m = DONE_LINE_RE.search(content)
    if m:
        return f"{content[:m.start()]}DONE: {new_body}"

    m = ACTION_DONE_RE.search(content)
    if m:
        return f"{content[:m.end()]}\nDONE: {new_body}"

    return f"DONE: {new_body}"
