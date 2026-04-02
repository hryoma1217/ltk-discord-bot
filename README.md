# LTK Discord Bot

LTK 向けの Discord BOT です。  
目的は、初対面メンバーが多いチームでも練習日程を合わせやすくし、確定後のリマインドまで自動化することです。

## できること

- リーダーが集計期限を設定
- 集計期限は `15m` `2h` `1d` `3h15m` などの相対時間で指定
- 期限を過ぎると自動で集計クローズ
- 集計期間の 50% 経過時点で途中経過をチャンネルへ投稿
- 締切 10 分前に未回答者だけへリマインド
- 未確定のまま期限切れなら BOT がキャンセル通知
- 候補日時ごとに `参加可 / 微妙 / 不可` を登録
- 各候補にコメントを残せる
  - 例: `22時からなら可`
  - 例: `途中参加なら可`
- 練習日程を確定
- 確定した日程に対して自動リマインド
- 手動リマインドも可能
- `practice_create` 時に対象メンバーと任意コーチを指定可能
- 作成時に対象者へメンション通知

## ディレクトリ構成

```text
ltk-discord-bot/
├─ bot.py
├─ storage.py
├─ requirements.txt
├─ .env.example
└─ README.md
```

## 前提

- Python 3.10 以上
- Discord Developer Portal で BOT 作成済み
- BOT に以下権限があること
  - `View Channels`
  - `Send Messages`
  - `Read Message History`
  - `Use Slash Commands`

## 推奨権限

最小権限で十分です。

- 必須
  - `View Channels`
  - `Send Messages`
  - `Read Message History`
  - `Use Slash Commands`

- 不要
  - `Administrator`
  - `Manage Server`
  - `Manage Roles`
  - `Manage Channels`
  - `Kick Members`
  - `Ban Members`
  - `Mention Everyone`
  - `Message Content Intent` を前提にした運用

## セキュリティメモ

- BOT トークンはコードにハードコーディングせず、環境変数 `DISCORD_BOT_TOKEN` から読み込みます
- この BOT は過去メッセージを収集・保存しません
- 保存対象は募集、対象メンバー、回答、コメント、リマインド履歴です
- サーバーごとのデータは `guild_id` で分離しています

## セットアップ

### 1. 仮想環境作成

```bash
python -m venv .venv
.venv\Scripts\activate
```

### 2. 依存インストール

```bash
pip install -r requirements.txt
```

### 3. 環境変数設定

`.env.example` を `.env` にコピーして編集してください。

```env
DISCORD_BOT_TOKEN=your_bot_token_here
LEADER_ROLE_NAMES=LTK運営,LTKリーダー
DATABASE_PATH=data/ltk_bot.sqlite3
DEFAULT_TIMEZONE=Asia/Tokyo
REMINDER_OFFSETS_MINUTES=1440,180,30
```

### 4. 起動

```bash
python bot.py
```

## コマンド一覧

### リーダー向け

- `/practice_create`
  - 練習候補を作成
  - 集計期限は `15m` / `2h` / `1d` / `3h15m` 形式
  - 対象メンバーと任意コーチを指定
- `/practice_confirm`
  - 確定候補を設定
- `/practice_close`
  - 募集を締切
- `/practice_remind`
  - 手動リマインド送信

### 閲覧・操作

- `/practice_list`
  - 募集一覧確認
- `/practice_show`
  - 募集詳細確認
- `/practice_help`
  - 使い方表示

## 練習候補の入力形式

`/practice_create` の `options_text` に、1行ごとに候補を入れます。  
あわせて `deadline_text` に相対時間で集計期限を入れます。

例:

```text
2026-04-05 21:00 | フルメン前提
2026-04-06 22:00 | 23時まで
04/07 21:30 | カスタム軽め
```

集計期限の例:

```text
30m
```

```text
24h
```

```text
1d
```

```text
3h15m
```

使える形式:

- `YYYY-MM-DD HH:MM`
- `YYYY/MM/DD HH:MM`
- `MM/DD HH:MM`
- `MM-DD HH:MM`

`|` の右側は候補メモとして保存されます。

## 使い方の流れ

1. リーダーが `/practice_create` で候補日時と集計期限を作成
2. 作成時に対象メンバーを指定
3. 指定された対象メンバーが通知メッセージのボタンUIで回答
4. リーダーが `/practice_show` で集計確認
5. `/practice_confirm` で日程確定
6. BOT が自動でリマインド

## 集計期限の動作

- リーダーが設定した `集計期限` を過ぎると、BOT が自動で募集をクローズします
- 期限時点で候補が未確定なら、BOT が以下の趣旨で通知します

```text
この予定は予定が合わないのでキャンセル！また集計してね。
```

- すでに候補を確定している場合は、キャンセルではなく「自動クローズ」として扱います

## 保存データ

SQLite に保存されます。

- メンバー一覧
- 練習募集
- 候補日時
- 回答状況
- コメント
- リマインド送信履歴

## サーバー分離

- 練習募集は Discord サーバー単位で分離されます
- 別サーバーの募集一覧や詳細が混ざらないよう、`guild_id` ベースで絞り込んでいます

## 補足

- リーダー判定は `LEADER_ROLE_NAMES` に指定したロール名、または Discord の管理権限を持つユーザーです
- 対象メンバー以外は通知メッセージのUIから回答できません
- コメントは候補ごとに1人1件で上書き保存です
