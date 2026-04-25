"""
FormAliases plugin for modmail-dev/Modmail.

Main idea:
    Every saved form becomes its own command
    Example: a form with alias `civ-vetting` can be sent inside a Modmail
    ticket by running:
        ?civ-vetting

Manager commands:
    ?formalias docs
    ?formalias civvetting
    ?formalias send civ-vetting
    ?formalias list
    ?formalias show civ-vetting
    ?formalias create <alias> <title>
    ?formalias set <alias> <title|intro|submit_message|button_label> <value>
    ?formalias addfield <alias> <short|paragraph> <yes/no required> <label>
    ?formalias removefield <alias> <index>
    ?formalias clearfields <alias>
    ?formalias delete <alias>
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel


CUSTOM_ID_PREFIX = "formalias"
MAX_MODAL_FIELDS = 5

# Keep this similar to your ArgAliases plugin so it is more tolerant across
# different Modmail permission enum names.
SUPPORTER_LEVEL = getattr(
    PermissionLevel,
    "SUPPORTER",
    getattr(PermissionLevel, "REGULAR", None),
)

MOD_LEVEL = getattr(
    PermissionLevel,
    "MODERATOR",
    getattr(
        PermissionLevel,
        "MOD",
        getattr(PermissionLevel, "SUPPORTER", getattr(PermissionLevel, "REGULAR", None)),
    ),
)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_alias(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = value.strip("-")
    return value[:32]


def field_id_from_label(label: str, existing: List[Dict[str, Any]]) -> str:
    base = re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_")[:30] or "field"
    taken = {field.get("id") for field in existing}
    candidate = base
    index = 2
    while candidate in taken:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def normalise_style(style: str) -> str:
    style = style.lower().strip()
    if style in {"long", "paragraph", "para", "multi", "textarea"}:
        return "paragraph"
    return "short"


def yes_no_text(value: bool) -> str:
    return "required" if value else "optional"


class ContinueFormView(discord.ui.View):
    def __init__(self, session_id: str, next_page: int):
        super().__init__(timeout=600)
        self.add_item(
            discord.ui.Button(
                label="Continue form",
                style=discord.ButtonStyle.primary,
                custom_id=f"{CUSTOM_ID_PREFIX}:continue:{session_id}:{next_page}",
            )
        )


class FormAliasModal(discord.ui.Modal):
    def __init__(
        self,
        plugin: "FormAliases",
        form: Dict[str, Any],
        session_id: str,
        page: int,
        page_fields: List[Tuple[int, Dict[str, Any]]],
    ):
        total_pages = max(1, (len(form.get("fields", [])) + MAX_MODAL_FIELDS - 1) // MAX_MODAL_FIELDS)
        title = truncate(form.get("title") or form.get("alias") or "Form", 35)
        if total_pages > 1:
            title = truncate(f"{title} {page + 1}/{total_pages}", 45)

        super().__init__(
            title=title,
            custom_id=f"{CUSTOM_ID_PREFIX}:modal:{form['alias']}:{session_id}:{page}",
            timeout=900,
        )
        self.plugin = plugin
        self.form = form
        self.session_id = session_id
        self.page = page
        self.page_fields = page_fields

        for absolute_index, field in page_fields:
            style_name = normalise_style(field.get("style", "short"))
            style = discord.TextStyle.paragraph if style_name == "paragraph" else discord.TextStyle.short
            max_length = int(field.get("max_length", 4000 if style_name == "paragraph" else 300))
            placeholder = field.get("placeholder") or None

            self.add_item(
                discord.ui.TextInput(
                    label=truncate(field.get("label", f"Question {absolute_index + 1}"), 45),
                    style=style,
                    required=bool(field.get("required", True)),
                    placeholder=truncate(placeholder, 100) if placeholder else None,
                    max_length=max_length,
                    custom_id=f"field_{absolute_index}",
                )
            )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values: Dict[str, str] = {}
        for child in self.children:
            if not isinstance(child, discord.ui.TextInput):
                continue

            try:
                absolute_index = int(str(child.custom_id).replace("field_", ""))
            except ValueError:
                continue

            field = self.form["fields"][absolute_index]
            field_id = field.get("id") or f"field_{absolute_index}"
            values[field_id] = str(child.value).strip()

        await self.plugin.handle_modal_submit(
            interaction=interaction,
            form=self.form,
            session_id=self.session_id,
            page=self.page,
            values=values,
        )


class FormAliases(commands.Cog):
    """Custom form aliases for Modmail threads."""

    def __init__(self, bot):
        self.bot = bot
        # Support both the docs pattern and the v4 community-plugin pattern.
        if hasattr(bot, "plugin_db") and hasattr(bot.plugin_db, "get_partition"):
            self.db = bot.plugin_db.get_partition(self)
        else:
            self.db = bot.api.get_plugin_partition(self)

        self.dynamic_commands: Dict[str, commands.Command] = {}

    async def cog_load(self):
        await self._load_dynamic_form_commands()

    async def cog_unload(self):
        self._unregister_all_dynamic_commands()

    def make_embed(self, *, title: str, description: Optional[str] = None, error: bool = False) -> discord.Embed:
        color = getattr(self.bot, "error_color", discord.Colour.red()) if error else getattr(self.bot, "main_color", discord.Colour.blurple())
        return discord.Embed(title=title, description=description, color=color)

    async def _load_dynamic_form_commands(self) -> None:
        self._unregister_all_dynamic_commands()
        async for form in self.db.find({"_id": {"$regex": r"^form:"}}):
            alias = form.get("alias")
            if alias:
                self._register_dynamic_command(alias, form)

    def _unregister_all_dynamic_commands(self) -> None:
        for name in list(self.dynamic_commands):
            self._unregister_dynamic_command(name)

    def _register_dynamic_command(self, name: str, form: Optional[Dict[str, Any]] = None) -> None:
        name = clean_alias(name)
        if not name or not NAME_RE.fullmatch(name):
            return

        existing = self.bot.get_command(name)
        if existing and name not in self.dynamic_commands:
            # Do not overwrite core Modmail commands or commands from other plugins.
            return

        if name in self.dynamic_commands:
            self._unregister_dynamic_command(name)

        async def dynamic_form(ctx: commands.Context, *, raw_args: str = ""):
            await self.send_form_by_alias(ctx, name)

        title = (form or {}).get("title") or name
        dynamic_form.__name__ = f"formalias_{name.replace('-', '_')}"

        decorated = checks.thread_only()(dynamic_form)
        decorated = checks.has_permissions(SUPPORTER_LEVEL)(decorated)

        command = commands.Command(
            decorated,
            name=name,
            help=f"Send the '{title}' form to the current Modmail recipient.",
            brief=f"Send form: {title}",
        )

        self.bot.add_command(command)
        self.dynamic_commands[name] = command

    def _unregister_dynamic_command(self, name: str) -> None:
        self.bot.remove_command(name)
        self.dynamic_commands.pop(name, None)

    def _validate_alias_name(self, alias: str) -> Optional[str]:
        if not NAME_RE.fullmatch(alias):
            return (
                "Alias names must be 1-32 characters and use only lowercase letters, "
                "numbers, `-`, or `_`, and must start with a letter or number."
            )

        existing = self.bot.get_command(alias)
        if existing and alias not in self.dynamic_commands:
            return f"`{alias}` already exists as a command, so it cannot be used as a form alias."

        return None

    def _get_primary_recipient(self, thread: Any):
        if hasattr(thread, "recipient") and thread.recipient:
            return thread.recipient
        recipients = getattr(thread, "recipients", None) or []
        return recipients[0] if recipients else None

    async def get_form(self, alias: str) -> Optional[Dict[str, Any]]:
        alias = clean_alias(alias)
        return await self.db.find_one({"_id": f"form:{alias}"})

    async def save_form(self, form: Dict[str, Any], *, register_command: bool = True) -> None:
        form["alias"] = clean_alias(form["alias"])
        form["_id"] = f"form:{form['alias']}"
        form["updated_at"] = now_iso()
        await self.db.find_one_and_update(
            {"_id": f"form:{form['alias']}"},
            {"$set": form, "$setOnInsert": {"created_at": now_iso()}},
            upsert=True,
        )
        if register_command:
            self._register_dynamic_command(form["alias"], form)

    def chunk_fields(self, form: Dict[str, Any], page: int) -> List[Tuple[int, Dict[str, Any]]]:
        fields = form.get("fields", [])
        start = page * MAX_MODAL_FIELDS
        selected = fields[start : start + MAX_MODAL_FIELDS]
        return list(enumerate(selected, start=start))

    async def start_session(
        self,
        *,
        interaction: discord.Interaction,
        form: Dict[str, Any],
        staff_channel_id: int,
        recipient_id: int,
    ) -> None:
        if interaction.user.id != recipient_id:
            await interaction.response.send_message("This form was not sent to you.", ephemeral=True)
            return

        if not form.get("fields"):
            await interaction.response.send_message("This form has no questions configured yet.", ephemeral=True)
            return

        session_id = secrets.token_urlsafe(8)
        await self.db.insert_one(
            {
                "_id": f"session:{session_id}",
                "session_id": session_id,
                "alias": form["alias"],
                "user_id": interaction.user.id,
                "staff_channel_id": staff_channel_id,
                "responses": {},
                "completed": False,
                "created_at": now_iso(),
            }
        )

        modal = FormAliasModal(self, form, session_id, 0, self.chunk_fields(form, 0))
        await interaction.response.send_modal(modal)

    async def continue_session(self, interaction: discord.Interaction, session_id: str, page: int) -> None:
        session = await self.db.find_one({"_id": f"session:{session_id}"})
        if not session:
            await interaction.response.send_message("That form session could not be found. Ask staff to send it again.", ephemeral=True)
            return

        if session.get("completed"):
            await interaction.response.send_message("This form has already been submitted.", ephemeral=True)
            return

        if interaction.user.id != int(session.get("user_id")):
            await interaction.response.send_message("This form session is not for you.", ephemeral=True)
            return

        form = await self.get_form(session["alias"])
        if not form:
            await interaction.response.send_message("This form no longer exists.", ephemeral=True)
            return

        modal = FormAliasModal(self, form, session_id, page, self.chunk_fields(form, page))
        await interaction.response.send_modal(modal)

    async def handle_modal_submit(
        self,
        *,
        interaction: discord.Interaction,
        form: Dict[str, Any],
        session_id: str,
        page: int,
        values: Dict[str, str],
    ) -> None:
        session = await self.db.find_one({"_id": f"session:{session_id}"})
        if not session:
            await interaction.response.send_message("That form session could not be found. Ask staff to send it again.", ephemeral=True)
            return

        if interaction.user.id != int(session.get("user_id")):
            await interaction.response.send_message("This form session is not for you.", ephemeral=True)
            return

        responses = dict(session.get("responses") or {})
        responses.update(values)

        fields = form.get("fields", [])
        total_pages = max(1, (len(fields) + MAX_MODAL_FIELDS - 1) // MAX_MODAL_FIELDS)
        next_page = page + 1

        await self.db.find_one_and_update(
            {"_id": f"session:{session_id}"},
            {"$set": {"responses": responses, "updated_at": now_iso()}},
        )

        if next_page < total_pages:
            await interaction.response.send_message(
                f"Page {page + 1}/{total_pages} saved. Click below to continue.",
                view=ContinueFormView(session_id, next_page),
            )
            return

        await self.finish_session(interaction, form, session_id, responses)

    async def finish_session(
        self,
        interaction: discord.Interaction,
        form: Dict[str, Any],
        session_id: str,
        responses: Dict[str, str],
    ) -> None:
        session = await self.db.find_one({"_id": f"session:{session_id}"}) or {}
        await self.db.find_one_and_update(
            {"_id": f"session:{session_id}"},
            {"$set": {"responses": responses, "completed": True, "completed_at": now_iso()}},
        )

        embed = discord.Embed(
            title=f"{form.get('title', form['alias'])} Response",
            color=getattr(self.bot, "main_color", discord.Colour.blurple()),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=str(interaction.user),
            icon_url=interaction.user.display_avatar.url if interaction.user.display_avatar else None,
        )
        embed.set_footer(text=f"User ID: {interaction.user.id} • Session: {session_id}")

        for index, field in enumerate(form.get("fields", []), start=1):
            field_id = field.get("id") or f"field_{index - 1}"
            answer = responses.get(field_id) or "No answer provided."
            embed.add_field(
                name=truncate(f"{index}. {field.get('label', field_id)}", 256),
                value=truncate(answer, 1024),
                inline=False,
            )

        staff_channel_id = int(session.get("staff_channel_id", 0) or 0)
        staff_channel = self.bot.get_channel(staff_channel_id)
        if staff_channel is None and staff_channel_id:
            try:
                staff_channel = await self.bot.fetch_channel(staff_channel_id)
            except discord.DiscordException:
                staff_channel = None

        posted = False
        if staff_channel is not None:
            try:
                await staff_channel.send(embed=embed)
                posted = True
            except discord.DiscordException:
                posted = False

        message = form.get("submit_message") or "Thank you. Your form has been submitted and will be reviewed by staff."
        if not posted:
            message += "\n\nHowever, I could not post the response into the staff ticket channel. Please let staff know."

        await interaction.response.send_message(message)

    async def send_form_by_alias(self, ctx: commands.Context, alias: str) -> None:
        form = await self.get_form(alias)
        if not form:
            await ctx.send(embed=self.make_embed(title="Form not found", description=f"`{alias}` does not exist.", error=True))
            return

        if not form.get("fields"):
            await ctx.send(embed=self.make_embed(title="No fields", description=f"`{alias}` has no questions configured yet.", error=True))
            return

        thread = getattr(ctx, "thread", None)
        if not thread:
            await ctx.send(embed=self.make_embed(title="Thread only", description="Forms can only be sent inside a Modmail ticket.", error=True))
            return

        recipient = self._get_primary_recipient(thread)
        if not recipient:
            await ctx.send(embed=self.make_embed(title="No recipient", description="I could not find the ticket recipient.", error=True))
            return

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label=truncate(form.get("button_label") or "Open Form", 80),
                style=discord.ButtonStyle.primary,
                custom_id=f"{CUSTOM_ID_PREFIX}:start:{form['alias']}:{ctx.channel.id}:{recipient.id}",
            )
        )

        embed = self.make_embed(
            title=form.get("title", form["alias"]),
            description=form.get("intro") or "Please click the button below to open the form.",
        )

        try:
            await recipient.send(embed=embed, view=view)
        except discord.Forbidden:
            await ctx.send(embed=self.make_embed(title="Could not DM user", description="The user has DMs disabled or blocked the bot.", error=True))
            return

        try:
            await ctx.message.add_reaction("✅")
        except discord.HTTPException:
            pass

        await ctx.send(embed=self.make_embed(title="Form sent", description=f"Sent `{form['alias']}` to {recipient.mention}."))

    def form_embed(self, form: Dict[str, Any]) -> discord.Embed:
        fields = form.get("fields", [])
        description = [
            f"**Command:** `?{form['alias']}`",
            f"**Alias:** `{form['alias']}`",
            f"**Title:** {form.get('title', 'Untitled')}",
            f"**Button:** {form.get('button_label', 'Open Form')}",
            "",
            "**Fields:**",
        ]
        if not fields:
            description.append("No fields configured.")
        else:
            for index, field in enumerate(fields, start=1):
                required = yes_no_text(bool(field.get("required", True)))
                description.append(f"{index}. `{field.get('style', 'short')}` {field.get('label')} ({required})")

        embed = self.make_embed(title="Form details", description="\n".join(description))
        embed.set_footer(text="Run this form directly inside a ticket using its command name.")
        return embed

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return

        data = interaction.data or {}
        custom_id = data.get("custom_id")
        if not custom_id or not str(custom_id).startswith(f"{CUSTOM_ID_PREFIX}:"):
            return

        parts = str(custom_id).split(":")
        action = parts[1] if len(parts) > 1 else None

        if action == "start" and len(parts) == 5:
            _, _, alias, staff_channel_id, recipient_id = parts
            form = await self.get_form(alias)
            if not form:
                await interaction.response.send_message("This form no longer exists.", ephemeral=True)
                return
            await self.start_session(
                interaction=interaction,
                form=form,
                staff_channel_id=int(staff_channel_id),
                recipient_id=int(recipient_id),
            )
            return

        if action == "continue" and len(parts) == 4:
            _, _, session_id, page = parts
            await self.continue_session(interaction, session_id, int(page))
            return

    @commands.group(
        name="formalias",
        aliases=["formaliases", "forms", "form", "falias", "faliases"],
        invoke_without_command=True,
    )
    @checks.has_permissions(SUPPORTER_LEVEL)
    async def formalias(self, ctx: commands.Context):
        """Manage custom Modmail form aliases."""
        await ctx.send_help(ctx.command)

    @formalias.command(name="docs", aliases=["help", "guide", "usage"])
    @checks.has_permissions(SUPPORTER_LEVEL)
    async def formalias_docs(self, ctx: commands.Context):
        """Show setup and usage docs for form aliases."""
        prefix = ctx.prefix
        embed = self.make_embed(
            title="Form alias docs",
            description=(
                "Form aliases let staff send modal forms from a Modmail ticket. "
                "Each saved form also becomes its own command, so you do not need to type `form send` every time.\n\n"
                f"**Example:** run `{prefix}civ-vetting` inside a ticket to DM the recipient the Civilian Vetting form."
            ),
        )
        embed.add_field(
            name="Quick setup",
            value=(
                f"`{prefix}formalias civvetting` — creates/updates the default civ vetting form\n"
                f"`{prefix}civ-vetting` — sends that form in the current ticket\n"
                f"`{prefix}formalias list` — lists all forms\n"
                f"`{prefix}formalias show civ-vetting` — shows the saved questions"
            ),
            inline=False,
        )
        embed.add_field(
            name="Create a custom form",
            value=(
                f"`{prefix}formalias create complaint Complaint Form`\n"
                f"`{prefix}formalias addfield complaint short yes Roblox Username / ID`\n"
                f"`{prefix}formalias addfield complaint paragraph yes What happened?`\n"
                f"Then run `{prefix}complaint` inside a ticket."
            ),
            inline=False,
        )
        embed.add_field(
            name="Field types",
            value="`short` = single-line answer\n`paragraph` = longer answer\nRequired can be `yes/true/1` or `no/false/0`.",
            inline=False,
        )
        embed.add_field(
            name="Management commands",
            value=(
                f"`{prefix}formalias set <alias> title <value>`\n"
                f"`{prefix}formalias set <alias> intro <value>`\n"
                f"`{prefix}formalias set <alias> button_label <value>`\n"
                f"`{prefix}formalias set <alias> submit_message <value>`\n"
                f"`{prefix}formalias removefield <alias> <number>`\n"
                f"`{prefix}formalias delete <alias>`"
            ),
            inline=False,
        )
        embed.set_footer(text="Forms can only be sent inside Modmail tickets because the bot needs a ticket recipient.")
        await ctx.send(embed=embed)

    @formalias.command(name="list")
    @checks.has_permissions(SUPPORTER_LEVEL)
    async def formalias_list(self, ctx: commands.Context):
        """List all form aliases."""
        forms = await self.db.find({"_id": {"$regex": r"^form:"}}).to_list(length=100)
        if not forms:
            await ctx.send(embed=self.make_embed(title="Form aliases", description="No forms have been created yet. Run `formalias docs` to get started."))
            return

        lines = []
        for form in sorted(forms, key=lambda item: item.get("alias", "")):
            command_status = "command enabled" if form.get("alias") in self.dynamic_commands else "use form send"
            lines.append(f"• `?{form['alias']}` — {form.get('title', 'Untitled')} ({len(form.get('fields', []))} fields, {command_status})")

        embed = self.make_embed(title="Form aliases", description="\n".join(lines[:25]))
        if len(lines) > 25:
            embed.set_footer(text=f"Showing 25 of {len(lines)} forms.")
        await ctx.send(embed=embed)

    @formalias.command(name="show", aliases=["view", "info"])
    @checks.has_permissions(SUPPORTER_LEVEL)
    async def formalias_show(self, ctx: commands.Context, alias: str):
        """Show the current configuration for a form."""
        form = await self.get_form(alias)
        if not form:
            await ctx.send(embed=self.make_embed(title="Form not found", description=f"`{alias}` does not exist.", error=True))
            return

        await ctx.send(embed=self.form_embed(form))

    @formalias.command(name="create", aliases=["add", "new"])
    @checks.has_permissions(MOD_LEVEL)
    async def formalias_create(self, ctx: commands.Context, alias: str, *, title: str):
        """Create a new form alias.

        Syntax:
        ?formalias create <alias> <title>
        """
        alias = clean_alias(alias)
        error = self._validate_alias_name(alias)
        if error:
            await ctx.send(embed=self.make_embed(title="Invalid alias", description=error, error=True))
            return

        existing = await self.get_form(alias)
        if existing:
            await ctx.send(embed=self.make_embed(title="Form already exists", description=f"`{alias}` already exists.", error=True))
            return

        form = {
            "_id": f"form:{alias}",
            "alias": alias,
            "title": title[:80],
            "intro": f"Please click the button below to open the {title} form.",
            "button_label": f"Open {truncate(title, 60)}",
            "submit_message": "Thank you. Your form has been submitted and will be reviewed by staff.",
            "fields": [],
            "created_by": ctx.author.id,
            "updated_by": ctx.author.id,
        }
        await self.save_form(form)
        await ctx.send(embed=self.make_embed(title="Form created", description=f"Created `{alias}`. Once fields are added, send it with `{ctx.prefix}{alias}` inside a ticket."))

    @formalias.command(name="delete", aliases=["remove", "del"])
    @checks.has_permissions(MOD_LEVEL)
    async def formalias_delete(self, ctx: commands.Context, alias: str):
        """Delete a form alias."""
        alias = clean_alias(alias)
        result = await self.db.delete_one({"_id": f"form:{alias}"})
        if result.deleted_count:
            self._unregister_dynamic_command(alias)
            await ctx.send(embed=self.make_embed(title="Form deleted", description=f"Deleted `{alias}` and removed the `{ctx.prefix}{alias}` command."))
        else:
            await ctx.send(embed=self.make_embed(title="Form not found", description=f"`{alias}` does not exist.", error=True))

    @formalias.command(name="set", aliases=["edit", "update"])
    @checks.has_permissions(MOD_LEVEL)
    async def formalias_set(self, ctx: commands.Context, alias: str, option: str, *, value: str):
        """Edit a form title, intro, button label or submit message."""
        form = await self.get_form(alias)
        if not form:
            await ctx.send(embed=self.make_embed(title="Form not found", description=f"`{alias}` does not exist.", error=True))
            return

        allowed = {
            "title": "title",
            "intro": "intro",
            "description": "intro",
            "submit": "submit_message",
            "submit_message": "submit_message",
            "button": "button_label",
            "button_label": "button_label",
            "button-label": "button_label",
        }
        key = allowed.get(option.lower())
        if key is None:
            await ctx.send(embed=self.make_embed(title="Invalid option", description="Use `title`, `intro`, `submit_message`, or `button_label`.", error=True))
            return

        form[key] = value
        form["updated_by"] = ctx.author.id
        await self.save_form(form)
        await ctx.send(embed=self.make_embed(title="Form updated", description=f"Updated `{key}` for `{form['alias']}`."))

    @formalias.command(name="addfield", aliases=["fieldadd", "add-question", "questionadd", "addq"])
    @checks.has_permissions(MOD_LEVEL)
    async def formalias_addfield(self, ctx: commands.Context, alias: str, style: str, required: bool, *, label: str):
        """Add a question to a form.

        Syntax:
        ?formalias addfield <alias> <short|paragraph> <yes/no> <label>
        """
        form = await self.get_form(alias)
        if not form:
            await ctx.send(embed=self.make_embed(title="Form not found", description=f"`{alias}` does not exist.", error=True))
            return

        fields = form.setdefault("fields", [])
        if len(fields) >= 25:
            await ctx.send(embed=self.make_embed(title="Too many fields", description="This plugin allows up to 25 fields per form.", error=True))
            return

        style = normalise_style(style)
        field = {
            "id": field_id_from_label(label, fields),
            "label": label[:90],
            "style": style,
            "required": required,
            "placeholder": "Yes/No" if "yes" in label.lower() or "no" in label.lower() else "",
            "max_length": 4000 if style == "paragraph" else 300,
        }
        fields.append(field)
        form["updated_by"] = ctx.author.id
        await self.save_form(form)
        await ctx.send(embed=self.make_embed(title="Field added", description=f"Added field {len(fields)} to `{form['alias']}`. Send it with `{ctx.prefix}{form['alias']}`."))

    @formalias.command(name="removefield", aliases=["fieldremove", "remove-question", "questionremove", "removeq"])
    @checks.has_permissions(MOD_LEVEL)
    async def formalias_removefield(self, ctx: commands.Context, alias: str, index: int):
        """Remove a question from a form by field number."""
        form = await self.get_form(alias)
        if not form:
            await ctx.send(embed=self.make_embed(title="Form not found", description=f"`{alias}` does not exist.", error=True))
            return

        fields = form.get("fields", [])
        if index < 1 or index > len(fields):
            await ctx.send(embed=self.make_embed(title="Invalid field", description="That field number does not exist.", error=True))
            return

        removed = fields.pop(index - 1)
        form["updated_by"] = ctx.author.id
        await self.save_form(form)
        await ctx.send(embed=self.make_embed(title="Field removed", description=f"Removed `{removed.get('label')}` from `{form['alias']}`."))

    @formalias.command(name="clearfields", aliases=["clear-fields"])
    @checks.has_permissions(MOD_LEVEL)
    async def formalias_clearfields(self, ctx: commands.Context, alias: str):
        """Remove every question from a form."""
        form = await self.get_form(alias)
        if not form:
            await ctx.send(embed=self.make_embed(title="Form not found", description=f"`{alias}` does not exist.", error=True))
            return

        form["fields"] = []
        form["updated_by"] = ctx.author.id
        await self.save_form(form)
        await ctx.send(embed=self.make_embed(title="Fields cleared", description=f"Cleared all fields from `{form['alias']}`."))

    @formalias.command(name="send")
    @checks.has_permissions(SUPPORTER_LEVEL)
    @checks.thread_only()
    async def formalias_send(self, ctx: commands.Context, alias: str):
        """Send a form by alias. Direct commands are preferred, for example ?civ-vetting."""
        await self.send_form_by_alias(ctx, alias)

    @formalias.command(name="reloadcommands", aliases=["reload", "refreshcommands"])
    @checks.has_permissions(MOD_LEVEL)
    async def formalias_reloadcommands(self, ctx: commands.Context):
        """Reload all dynamic form commands from the database."""
        await self._load_dynamic_form_commands()
        await ctx.send(embed=self.make_embed(title="Form commands reloaded", description=f"Loaded {len(self.dynamic_commands)} direct form commands."))

    @formalias.command(name="civvetting", aliases=["setupciv", "civ", "civ-vetting-setup"])
    @checks.has_permissions(MOD_LEVEL)
    async def formalias_civvetting(self, ctx: commands.Context):
        """Create or update the default Nottinghamshire Civilian Vetting form."""
        alias = "civ-vetting"
        fields: List[Dict[str, Any]] = []

        def add(label: str, style: str = "short", required: bool = True, placeholder: str = "") -> None:
            fields.append(
                {
                    "id": field_id_from_label(label, fields),
                    "label": label,
                    "style": style,
                    "required": required,
                    "placeholder": placeholder,
                    "max_length": 4000 if style == "paragraph" else 300,
                }
            )

        add("Roblox Username / ID")
        add("Discord ID")
        add("Previous Member?", placeholder="Yes/No")
        add("Any Previous Moderation History?", placeholder="Yes/No")
        add("If yes, what was it?", style="paragraph", required=False)
        add("Have you read Nottinghamshire's in-game and Discord server rules yet?", placeholder="Yes/No")
        add("Do you have a microphone that works and are you willing to use voice channels to roleplay?", placeholder="Yes/No")

        form = {
            "_id": f"form:{alias}",
            "alias": alias,
            "title": "Civilian Vetting",
            "intro": "Please click the button below to open the Civilian Vetting form.",
            "button_label": "Open Civilian Vetting Form",
            "submit_message": "Thank you. Your Civilian Vetting form has been submitted and will be reviewed by staff.",
            "fields": fields,
            "updated_by": ctx.author.id,
        }
        await self.save_form(form)
        await ctx.send(
            embed=self.make_embed(
                title="Civ vetting form saved",
                description=f"Use `{ctx.prefix}civ-vetting` inside a Modmail ticket to send it. You can still use `{ctx.prefix}formalias send civ-vetting` if needed.",
            )
        )


async def setup(bot):
    await bot.add_cog(FormAliases(bot))
