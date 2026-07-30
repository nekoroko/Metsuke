// src/brand.ts — サービス名の唯一の定義箇所。
//
// 画面のあちこちに名前をベタ書きすると、次に名前が変わったときに
// 必ず取りこぼす（実際、旧名は Shell / index.html / README / API の
// タイトルに散っていた）。表示に使う名前はここからしか読まない。
//
// 内部識別子（DBファイル名 agent_studio.db、環境変数 AGENT_STUDIO_DB、
// サンドボックスイメージ agent-studio-sandbox:latest、Python の
// モジュール名）は**ここには含めない**。それらは既存インストールの
// 移行に直結するので、表示名の変更とは切り離してある。

/** 表示に使うサービス名。 */
export const BRAND_NAME = 'Metsuke'

/** サイドバーなど、大文字で見せる場所用。 */
export const BRAND_NAME_UPPER = 'METSUKE'

/** 名前の下に添える一行。 */
export const BRAND_TAGLINE = 'LOCAL-FIRST AGENT RUNTIME'

/** 「これは何か」を1文で。ヘルプと about に使う。 */
export const BRAND_DESCRIPTION =
  'ローカルLLM でも API でも動く、自律型タスク実行エージェント基盤。'

/** ブラウザのタブに出す文字列。 */
export function pageTitle(section?: string): string {
  return section ? `${section} · ${BRAND_NAME}` : BRAND_NAME
}
