from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from storage import Storage


STATUS_LABELS = {
    "available": "参加可",
    "maybe": "微妙",
    "unavailable": "不可",
}


@dataclass
class BotConfig:
    token: str
    leader_role_names: list[str]
    db_path: str
    timezone: ZoneInfo
    reminder_offsets_minutes: list[int]


def load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def load_config() -> BotConfig:
    load_dotenv()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required")

    role_names = [v.strip() for v in os.environ.get("LEADER_ROLE_NAMES", "").split(",") if v.strip()]
    db_path = os.environ.get("DATABASE_PATH", "data/ltk_bot.sqlite3").strip()
    timezone = ZoneInfo(os.environ.get("DEFAULT_TIMEZONE", "Asia/Tokyo").strip())
    reminder_offsets = [
        int(v.strip())
        for v in os.environ.get("REMINDER_OFFSETS_MINUTES", "1440,180,30").split(",")
        if v.strip()
    ]
    reminder_offsets.sort(reverse=True)
    return BotConfig(token, role_names, db_path, timezone, reminder_offsets)


def now_jst(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def parse_datetime_line(raw: str, tz: ZoneInfo) -> tuple[datetime, str | None]:
    text = raw.strip()
    note = None
    if " | " in text:
        dt_part, note = text.split(" | ", 1)
    elif "|" in text:
        dt_part, note = text.split("|", 1)
    else:
        dt_part = text

    dt_part = dt_part.strip()
    note = note.strip() if note else None

    current = now_jst(tz)
    formats = [
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%m/%d %H:%M",
        "%m-%d %H:%M",
    ]
    last_error = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(dt_part, fmt)
            if "%Y" not in fmt:
                parsed = parsed.replace(year=current.year)
                if parsed < current - timedelta(days=1):
                    parsed = parsed.replace(year=current.year + 1)
            parsed = parsed.replace(tzinfo=tz)
            return parsed, note
        except ValueError as exc:
            last_error = exc
    raise ValueError(f"日時を解釈できません: {raw}") from last_error


def parse_single_datetime(raw: str, tz: ZoneInfo) -> datetime:
    parsed, _ = parse_datetime_line(raw, tz)
    return parsed


def parse_deadline_offset(raw: str) -> timedelta:
    text = raw.strip().lower()
    match = re.fullmatch(r"(\d+)\s*([mh])", text)
    if not match:
        raise ValueError("集計期限は `30m` または `24h` の形式で入力してください。")
    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        raise ValueError("集計期限は 1 以上で指定してください。")
    if unit == "m":
        return timedelta(minutes=amount)
    return timedelta(hours=amount)


class PracticeBot(commands.Bot):
    def __init__(self, config: BotConfig) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.config_data = config
        self.storage = Storage(config.db_path)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        self.reminder_loop.start()

    async def close(self) -> None:
        self.reminder_loop.cancel()
        await super().close()

    def is_leader(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
        if not self.config_data.leader_role_names:
            return False
        member_roles = {role.name for role in member.roles}
        return any(name in member_roles for name in self.config_data.leader_role_names)

    def get_registered_member(self, user_id: int):
        return self.storage.get_member(user_id)

    async def ensure_leader(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if not isinstance(user, discord.Member) or not self.is_leader(user):
            await interaction.response.send_message("この操作はリーダーのみ実行できます。", ephemeral=True)
            return False
        return True

    async def ensure_target_member(self, interaction: discord.Interaction, practice_id: int) -> bool:
        if not self.storage.is_practice_target(practice_id, interaction.user.id):
            await interaction.response.send_message("この募集で指定された対象メンバーのみ回答できます。", ephemeral=True)
            return False
        return True

    def build_practice_summary(self, practice_id: int) -> str:
        practice = self.storage.get_practice(practice_id)
        if not practice:
            return "募集が見つかりません。"

        options = self.storage.get_practice_options(practice_id)
        targets = self.storage.list_practice_targets(practice_id)
        target_map = {target.user_id: target for target in targets}
        target_ids = set(target_map.keys())

        lines = [
            f"**募集ID:** {practice.id}",
            f"**タイトル:** {practice.title}",
        ]
        if practice.description:
            lines.append(f"**説明:** {practice.description}")
        if practice.collect_deadline:
            deadline = datetime.fromisoformat(practice.collect_deadline)
            lines.append(f"**集計期限:** {format_dt(deadline)}")
        lines.append(f"**状態:** {'締切済み' if practice.is_closed else '募集中'}")
        if practice.closed_reason:
            lines.append(f"**クローズ理由:** {practice.closed_reason}")
        if targets:
            member_names = [target.display_name for target in targets if target.role_kind == "member"]
            coach_names = [target.display_name for target in targets if target.role_kind == "coach"]
            if member_names:
                lines.append(f"**対象メンバー:** {', '.join(member_names)}")
            if coach_names:
                lines.append(f"**コーチ:** {', '.join(coach_names)}")
        lines.append("")

        for option in options:
            dt = datetime.fromisoformat(option.starts_at)
            responses = self.storage.get_responses_for_option(option.id)
            by_status = {"available": [], "maybe": [], "unavailable": []}
            responded_ids = set()
            comment_lines = []
            for row in responses:
                uid = int(row["user_id"])
                if uid not in target_ids:
                    continue
                responded_ids.add(uid)
                label = row["display_name"] or f"User:{uid}"
                status = row["status"]
                by_status.setdefault(status, []).append(label)
                if row["comment"]:
                    comment_lines.append(f"- {label}: {row['comment']}")
            pending = [target_map[uid].display_name for uid in sorted(target_ids - responded_ids)]

            prefix = "✅" if option.is_confirmed else "・"
            lines.append(f"{prefix} 候補{option.option_no}: {format_dt(dt)}")
            if option.note:
                lines.append(f"  備考: {option.note}")
            lines.append(f"  参加可 ({len(by_status['available'])}): {', '.join(by_status['available']) or 'なし'}")
            lines.append(f"  微妙 ({len(by_status['maybe'])}): {', '.join(by_status['maybe']) or 'なし'}")
            lines.append(f"  不可 ({len(by_status['unavailable'])}): {', '.join(by_status['unavailable']) or 'なし'}")
            lines.append(f"  未回答 ({len(pending)}): {', '.join(pending) or 'なし'}")
            if comment_lines:
                lines.append("  コメント:")
                lines.extend(f"    {line}" for line in comment_lines)
            lines.append("")
        return "\n".join(lines).strip()

    @tasks.loop(minutes=1)
    async def reminder_loop(self) -> None:
        await self.wait_until_ready()
        current = now_jst(self.config_data.timezone)
        await self._close_expired_practices(current)
        rows = self.storage.get_confirmed_options()
        for row in rows:
            starts_at = datetime.fromisoformat(row["starts_at"])
            if starts_at.tzinfo is None:
                starts_at = starts_at.replace(tzinfo=self.config_data.timezone)
            for minutes in self.config_data.reminder_offsets_minutes:
                remind_at = starts_at - timedelta(minutes=minutes)
                if current < remind_at or current > remind_at + timedelta(minutes=1):
                    continue
                option_id = int(row["option_id"])
                if self.storage.was_reminder_sent(option_id, minutes):
                    continue
                channel = self.get_channel(int(row["channel_id"]))
                if channel is None:
                    try:
                        channel = await self.fetch_channel(int(row["channel_id"]))
                    except discord.HTTPException:
                        continue
                if not isinstance(channel, discord.abc.Messageable):
                    continue

                responses = self.storage.get_responses_for_option(option_id)
                available_mentions = []
                maybe_mentions = []
                for response in responses:
                    mention = f"<@{int(response['user_id'])}>"
                    if response["status"] == "available":
                        available_mentions.append(mention)
                    elif response["status"] == "maybe":
                        maybe_mentions.append(mention)

                message_lines = [
                    f"⏰ **練習リマインド** `{row['title']}`",
                    f"日時: {format_dt(starts_at)}",
                    f"候補: {row['option_no']}",
                    f"{minutes}分前です。",
                ]
                if row["note"]:
                    message_lines.append(f"備考: {row['note']}")
                if available_mentions:
                    message_lines.append(f"参加可: {' '.join(available_mentions)}")
                if maybe_mentions:
                    message_lines.append(f"微妙: {' '.join(maybe_mentions)}")

                await channel.send("\n".join(message_lines))
                self.storage.mark_reminder_sent(option_id, minutes, current.isoformat())

    async def _close_expired_practices(self, current: datetime) -> None:
        expired = self.storage.get_expired_open_practices(current.isoformat())
        for practice in expired:
            options = self.storage.get_practice_options(int(practice["id"]))
            confirmed = next((opt for opt in options if opt.is_confirmed), None)
            channel = self.get_channel(int(practice["channel_id"]))
            if channel is None:
                try:
                    channel = await self.fetch_channel(int(practice["channel_id"]))
                except discord.HTTPException:
                    channel = None

            if confirmed is None:
                self.storage.close_practice(int(practice["id"]), "集計期限切れのため自動キャンセル")
                if isinstance(channel, discord.abc.Messageable):
                    await channel.send(
                        f"📌 `{practice['title']}` は集計期限を過ぎたためクローズしました。\n"
                        "この予定は予定が合わないのでキャンセル！また集計してね。"
                    )
            else:
                self.storage.close_practice(int(practice["id"]), "集計期限経過のため自動クローズ")
                if isinstance(channel, discord.abc.Messageable):
                    dt = datetime.fromisoformat(confirmed.starts_at)
                    await channel.send(
                        f"📌 `{practice['title']}` は集計期限を過ぎたため自動クローズしました。\n"
                        f"確定日程: {format_dt(dt)}"
                    )

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.wait_until_ready()


bot = PracticeBot(load_config())


@bot.tree.command(name="member_add", description="登録メンバーを追加または更新します")
@app_commands.describe(user="登録するメンバー", role_label="ロールや担当", note="備考")
async def member_add(interaction: discord.Interaction, user: discord.Member, role_label: str | None = None, note: str | None = None):
    if not await bot.ensure_leader(interaction):
        return
    bot.storage.add_member(user.id, user.display_name, role_label, note, now_jst(bot.config_data.timezone).isoformat())
    await interaction.response.send_message(f"{user.mention} を登録しました。", ephemeral=True)


@bot.tree.command(name="member_remove", description="登録メンバーを削除します")
async def member_remove(interaction: discord.Interaction, user: discord.Member):
    if not await bot.ensure_leader(interaction):
        return
    ok = bot.storage.remove_member(user.id)
    await interaction.response.send_message(
        f"{user.mention} を削除しました。" if ok else "登録されていません。",
        ephemeral=True,
    )


@bot.tree.command(name="member_list", description="登録済みメンバーを一覧表示します")
async def member_list(interaction: discord.Interaction):
    members = bot.storage.list_members()
    if not members:
        await interaction.response.send_message("登録メンバーはまだいません。", ephemeral=True)
        return
    lines = ["**登録メンバー一覧**"]
    for member in members:
        extra = []
        if member.role_label:
            extra.append(member.role_label)
        if member.note:
            extra.append(member.note)
        suffix = f" ({' / '.join(extra)})" if extra else ""
        lines.append(f"- {member.display_name}{suffix}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="practice_create", description="練習日程の候補を作成します")
@app_commands.describe(
    title="募集タイトル",
    options_text="候補日時を1行ずつ入力。例: 2026-04-05 21:00 | スクリム",
    deadline_text="集計期限。例: 30m または 24h",
    description="説明やメモ",
    member1="対象メンバー1",
    member2="対象メンバー2",
    member3="対象メンバー3",
    member4="対象メンバー4",
    member5="対象メンバー5",
    member6="対象メンバー6",
    member7="対象メンバー7",
    member8="対象メンバー8",
    coach="任意のコーチ",
)
async def practice_create(
    interaction: discord.Interaction,
    title: str,
    options_text: str,
    deadline_text: str,
    description: str | None = None,
    member1: discord.Member | None = None,
    member2: discord.Member | None = None,
    member3: discord.Member | None = None,
    member4: discord.Member | None = None,
    member5: discord.Member | None = None,
    member6: discord.Member | None = None,
    member7: discord.Member | None = None,
    member8: discord.Member | None = None,
    coach: discord.Member | None = None,
):
    if not await bot.ensure_leader(interaction):
        return
    option_lines = [line.strip() for line in options_text.splitlines() if line.strip()]
    if not option_lines:
        await interaction.response.send_message("候補日時を1件以上入力してください。", ephemeral=True)
        return

    parsed_options: list[tuple[int, str, str | None]] = []
    created_at = now_jst(bot.config_data.timezone)
    try:
        deadline = created_at + parse_deadline_offset(deadline_text)
        for idx, line in enumerate(option_lines, start=1):
            dt, note = parse_datetime_line(line, bot.config_data.timezone)
            parsed_options.append((idx, dt.isoformat(), note))
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    if any(datetime.fromisoformat(item[1]) <= deadline for item in parsed_options):
        await interaction.response.send_message("集計期限は、すべての候補日時より前にしてください。", ephemeral=True)
        return

    raw_members = [member1, member2, member3, member4, member5, member6, member7, member8]
    targets: list[tuple[int, str, str, int]] = []
    seen_ids: set[int] = set()
    for sort_order, member in enumerate((m for m in raw_members if m is not None), start=1):
        if member.id in seen_ids:
            continue
        seen_ids.add(member.id)
        targets.append((member.id, member.display_name, "member", sort_order))
        bot.storage.add_member(member.id, member.display_name, "member", None, created_at.isoformat())
    if coach and coach.id not in seen_ids:
        targets.append((coach.id, coach.display_name, "coach", len(targets) + 1))
        bot.storage.add_member(coach.id, coach.display_name, "coach", None, created_at.isoformat())
    if not targets:
        await interaction.response.send_message("対象メンバーを1人以上指定してください。", ephemeral=True)
        return

    practice_id = bot.storage.create_practice(
        title=title,
        description=description,
        channel_id=interaction.channel_id or 0,
        created_by=interaction.user.id,
        created_at=created_at.isoformat(),
        collect_deadline=deadline.isoformat(),
        options=parsed_options,
        targets=targets,
    )
    summary = bot.build_practice_summary(practice_id)
    mentions = " ".join(f"<@{user_id}>" for user_id, _, _, _ in targets)
    await interaction.response.send_message(
        f"{mentions}\n日程調整を作成しました。\n\n{summary}\n\n"
        "対象メンバーは `/availability_set` で回答してください。",
        allowed_mentions=discord.AllowedMentions(users=True),
    )


@bot.tree.command(name="practice_list", description="日程調整の一覧を表示します")
async def practice_list(interaction: discord.Interaction, include_closed: bool = False):
    practices = bot.storage.list_practices(include_closed=include_closed)
    if not practices:
        await interaction.response.send_message("日程調整はまだありません。", ephemeral=True)
        return
    lines = ["**日程調整一覧**"]
    for practice in practices[:20]:
        status = "締切済み" if practice.is_closed else "募集中"
        deadline = ""
        if practice.collect_deadline:
            deadline = f" | 期限:{format_dt(datetime.fromisoformat(practice.collect_deadline))}"
        lines.append(f"- ID:{practice.id} | {practice.title} | {status}{deadline}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="practice_show", description="指定した日程調整の詳細を表示します")
async def practice_show(interaction: discord.Interaction, practice_id: int):
    practice = bot.storage.get_practice(practice_id)
    if not practice:
        await interaction.response.send_message("指定の募集が見つかりません。", ephemeral=True)
        return
    await interaction.response.send_message(
        bot.build_practice_summary(practice_id),
        allowed_mentions=discord.AllowedMentions.none(),
        ephemeral=True,
    )


@bot.tree.command(name="availability_set", description="自分の参加可否を登録します")
@app_commands.describe(
    practice_id="募集ID",
    option_no="候補番号",
    status="参加状態",
    comment="コメント（例: 22時からなら可）",
)
async def availability_set(
    interaction: discord.Interaction,
    practice_id: int,
    option_no: int,
    status: Literal["available", "maybe", "unavailable"],
    comment: str | None = None,
):
    practice = bot.storage.get_practice(practice_id)
    if not practice:
        await interaction.response.send_message("指定の募集が見つかりません。", ephemeral=True)
        return
    if not await bot.ensure_target_member(interaction, practice_id):
        return
    options = bot.storage.get_practice_options(practice_id)
    option = next((opt for opt in options if opt.option_no == option_no), None)
    if option is None:
        await interaction.response.send_message("候補番号が見つかりません。", ephemeral=True)
        return
    bot.storage.set_availability(option.id, interaction.user.id, status, comment, now_jst(bot.config_data.timezone).isoformat())
    await interaction.response.send_message(
        f"候補{option_no} に `{STATUS_LABELS[status]}` で回答しました。",
        ephemeral=True,
    )


@bot.tree.command(name="practice_close", description="日程調整を締め切ります")
async def practice_close(interaction: discord.Interaction, practice_id: int):
    if not await bot.ensure_leader(interaction):
        return
    ok = bot.storage.close_practice(practice_id)
    await interaction.response.send_message("締め切りました。" if ok else "募集が見つかりません。", ephemeral=True)


@bot.tree.command(name="practice_confirm", description="確定した候補を設定します")
async def practice_confirm(interaction: discord.Interaction, practice_id: int, option_no: int):
    if not await bot.ensure_leader(interaction):
        return
    ok = bot.storage.set_confirmed_option(practice_id, option_no)
    if not ok:
        await interaction.response.send_message("指定の候補が見つかりません。", ephemeral=True)
        return
    await interaction.response.send_message(
        f"募集ID {practice_id} の候補{option_no} を確定しました。リマインド対象になります。"
    )


@bot.tree.command(name="practice_remind", description="確定済み候補のリマインドを手動送信します")
async def practice_remind(interaction: discord.Interaction, practice_id: int):
    if not await bot.ensure_leader(interaction):
        return
    practice = bot.storage.get_practice(practice_id)
    if not practice:
        await interaction.response.send_message("募集が見つかりません。", ephemeral=True)
        return
    options = [opt for opt in bot.storage.get_practice_options(practice_id) if opt.is_confirmed]
    if not options:
        await interaction.response.send_message("確定した候補がありません。", ephemeral=True)
        return
    option = options[0]
    responses = bot.storage.get_responses_for_option(option.id)
    available_mentions = []
    maybe_mentions = []
    for response in responses:
        mention = f"<@{int(response['user_id'])}>"
        if response["status"] == "available":
            available_mentions.append(mention)
        elif response["status"] == "maybe":
            maybe_mentions.append(mention)
    dt = datetime.fromisoformat(option.starts_at)
    lines = [
        f"📣 **手動リマインド** `{practice.title}`",
        f"日時: {format_dt(dt)}",
    ]
    if option.note:
        lines.append(f"備考: {option.note}")
    if available_mentions:
        lines.append(f"参加可: {' '.join(available_mentions)}")
    if maybe_mentions:
        lines.append(f"微妙: {' '.join(maybe_mentions)}")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="practice_help", description="使い方を表示します")
async def practice_help(interaction: discord.Interaction):
    message = (
        "**LTK 練習調整BOT 使い方**\n"
        "リーダー:\n"
        "- /member_add\n"
        "- /member_remove\n"
        "- /practice_create （集計期限は 30m / 24h 形式、対象メンバー指定つき）\n"
        "- /practice_confirm\n"
        "- /practice_close\n"
        "- /practice_remind\n\n"
        "メンバー:\n"
        "- /practice_list\n"
        "- /practice_show\n"
        "- /availability_set\n"
    )
    await interaction.response.send_message(message, ephemeral=True)


def main() -> None:
    bot.run(bot.config_data.token)


if __name__ == "__main__":
    main()
