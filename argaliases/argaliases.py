import re
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel


MOD_LEVEL = getattr(
    PermissionLevel,
    "MODERATOR",
    getattr(
        PermissionLevel,
        "MOD",
        getattr(
            PermissionLevel,
            "SUPPORTER",
            getattr(PermissionLevel, "REGULAR", None),
        ),
    ),
)

ADMIN_LEVEL = getattr(
    PermissionLevel,
    "ADMINISTRATOR",
    getattr(PermissionLevel, "ADMIN", MOD_LEVEL),
)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*|\d+\+?|args)\}")
VALID_MODES = {"reply", "anonreply", "plainreply", "note"}


class ArgAliases(commands.Cog):
    """Argument-aware custom aliases for Modmail."""

    def __init__(self, bot):
        self.bot = bot
        # Support both the docs pattern and the v4 community-plugin pattern.
        if hasattr(bot, "plugin_db") and hasattr(bot.plugin_db, "get_partition"):
            self.db = bot.plugin_db.get_partition(self)
        else:
            self.db = bot.api.get_plugin_partition(self)

        self.aliases: Dict[str, Dict[str, Any]] = {}
        self.dynamic_commands: Dict[str, commands.Command] = {}

    async def cog_load(self):
        await self._load_aliases()

    async def cog_unload(self):
        self._unregister_all_dynamic_commands()

    async def _load_aliases(self) -> None:
        self.aliases.clear()
        self._unregister_all_dynamic_commands()

        cursor = self.db.find({"_id": {"$regex": r"^argalias:"}})
        async for doc in cursor:
            name = doc.get("name")
            if not name:
                continue
            self.aliases[name] = doc
            self._register_dynamic_command(name)

    def _unregister_all_dynamic_commands(self) -> None:
        for name in list(self.dynamic_commands):
            self._unregister_dynamic_command(name)

    def _register_dynamic_command(self, name: str) -> None:
        alias = self.aliases.get(name)
        if not alias:
            return

        if name in self.dynamic_commands:
            self._unregister_dynamic_command(name)

        async def dynamic_alias(cog, ctx: commands.Context, *, raw_args: str = ""):
            await cog._execute_alias(ctx, name, raw_args)

        dynamic_alias.__name__ = f"argalias_{name}"
        decorated = checks.has_permissions(MOD_LEVEL)(dynamic_alias)
        command = commands.Command(
            decorated,
            name=name,
            help=f"Dynamic argument alias: {name}",
            brief=alias.get("description") or f"Runs the '{name}' argument alias.",
        )
        command.cog = self

        self.bot.add_command(command)
        self.dynamic_commands[name] = command

    def _unregister_dynamic_command(self, name: str) -> None:
        self.bot.remove_command(name)
        self.dynamic_commands.pop(name, None)

    def _extract_prefix(self, text: str) -> Tuple[Optional[str], str]:
        stripped = text.strip()
        if not stripped:
            return None, ""

        first_space = stripped.find(" ")
        if first_space == -1:
            return stripped.lower(), ""

        first = stripped[:first_space].lower()
        rest = stripped[first_space + 1 :].lstrip()
        return first, rest

    def _parse_template_input(self, raw: str) -> Tuple[str, str]:
        first, rest = self._extract_prefix(raw)
        if first in VALID_MODES and rest:
            return first, rest
        return "reply", raw.strip()

    async def _alias_exists_in_db(self, name: str) -> bool:
        return await self.db.find_one({"_id": f"argalias:{name}"}) is not None

    def _validate_name(self, name: str) -> Optional[str]:
        name = name.lower().strip()
        if not NAME_RE.fullmatch(name):
            return (
                "Alias names must be 1-32 characters and use only lowercase letters, "
                "numbers, `-`, or `_`, and must start with a letter or number."
            )

        existing = self.bot.get_command(name)
        if existing and name not in self.dynamic_commands:
            return f"`{name}` already exists as a command, so it cannot be used as an argument alias."

        return None

    async def _save_alias(self, payload: Dict[str, Any]) -> None:
        await self.db.find_one_and_update(
            {"_id": f"argalias:{payload['name']}"},
            {"$set": payload},
            upsert=True,
        )

    async def _delete_alias(self, name: str) -> None:
        await self.db.delete_one({"_id": f"argalias:{name}"})

    def _get_primary_recipient(self, thread: Any):
        if hasattr(thread, "recipient") and thread.recipient:
            return thread.recipient
        recipients = getattr(thread, "recipients", None) or []
        return recipients[0] if recipients else None

    def _render_template(self, template: str, ctx: commands.Context, raw_args: str) -> str:
        thread = getattr(ctx, "thread", None)
        recipient = self._get_primary_recipient(thread) if thread else None
        pieces = raw_args.split() if raw_args else []

        replacements = {
            "args": raw_args,
            "author": getattr(ctx.author, "display_name", ctx.author.name),
            "author_name": ctx.author.name,
            "author_mention": ctx.author.mention,
            "author_id": str(ctx.author.id),
            "channel": getattr(ctx.channel, "mention", getattr(ctx.channel, "name", "")),
            "channel_name": getattr(ctx.channel, "name", ""),
            "recipient": getattr(recipient, "display_name", ""),
            "recipient_name": getattr(recipient, "name", ""),
            "recipient_mention": getattr(recipient, "mention", ""),
            "recipient_id": str(getattr(recipient, "id", "")) if recipient else "",
            "newline": "\n",
        }

        def replace(match: re.Match) -> str:
            token = match.group(1)

            if token in replacements:
                return replacements[token]

            if token.isdigit():
                index = int(token) - 1
                return pieces[index] if 0 <= index < len(pieces) else ""

            if token.endswith("+") and token[:-1].isdigit():
                index = int(token[:-1]) - 1
                return " ".join(pieces[index:]) if 0 <= index < len(pieces) else ""

            return match.group(0)

        return TOKEN_RE.sub(replace, template)

    async def _execute_alias(self, ctx: commands.Context, name: str, raw_args: str) -> None:
        alias = self.aliases.get(name)
        if not alias:
            return await ctx.send(f"Argument alias `{name}` no longer exists.")

        mode = alias.get("mode", "reply")
        template = alias.get("template", "")
        content = self._render_template(template, ctx, raw_args.strip())

        if mode != "note" and not getattr(ctx, "thread", None):
            return await ctx.send(
                f"`{name}` is set to `{mode}` and can only be used inside a Modmail thread."
            )

        if mode == "reply":
            await ctx.thread.reply(ctx.message, content)
        elif mode == "anonreply":
            await ctx.thread.reply(ctx.message, content, anonymous=True)
        elif mode == "plainreply":
            await ctx.thread.reply(ctx.message, content, plain=True)
        elif mode == "note":
            await ctx.send(content)
        else:
            return await ctx.send(
                f"`{name}` has an invalid mode (`{mode}`). Edit it with `?argaliases edit {name} ...`."
            )

        try:
            await ctx.message.add_reaction("✅")
        except discord.HTTPException:
            pass

    def _alias_embed(self, alias: Dict[str, Any]) -> discord.Embed:
        embed = discord.Embed(
            title=f"Argument alias: {alias['name']}",
            colour=getattr(self.bot, "main_color", discord.Colour.blurple()),
            description=alias.get("description") or "No description set.",
        )
        embed.add_field(name="Mode", value=alias.get("mode", "reply"), inline=True)
        embed.add_field(name="Created by", value=f"<@{alias.get('created_by', 0)}>", inline=True)
        embed.add_field(name="Updated by", value=f"<@{alias.get('updated_by', 0)}>", inline=True)
        template = alias.get("template", "")
        if len(template) > 1000:
            template = template[:997] + "..."
        embed.add_field(name="Template", value=f"```\n{template}\n```", inline=False)
        embed.set_footer(text="Placeholders: {args}, {1}, {2}, {1+}, {author}, {recipient}, {newline}")
        return embed

    @checks.has_permissions(MOD_LEVEL)
    @commands.group(
        name="argaliases",
        aliases=["argalias", "argalises", "argalise", "aaliases", "aalias"],
        invoke_without_command=True,
    )
    async def argaliases(self, ctx: commands.Context):
        """Manage argument-aware aliases."""
        await ctx.send_help(ctx.command)

    @checks.has_permissions(MOD_LEVEL)
    @argaliases.command(name="create", aliases=["add", "new"])
    async def argaliases_create(self, ctx: commands.Context, name: str, *, raw: str):
        """Create an argument alias.

        Syntax:
        ?argaliases create <name> [mode] <template>
        """
        name = name.lower().strip()
        error = self._validate_name(name)
        if error:
            return await ctx.send(error)

        if await self._alias_exists_in_db(name):
            return await ctx.send(f"`{name}` already exists. Use `?argaliases edit {name} ...` instead.")

        mode, template = self._parse_template_input(raw)
        if not template:
            return await ctx.send("Please provide a template for the alias.")

        payload = {
            "_id": f"argalias:{name}",
            "name": name,
            "mode": mode,
            "template": template,
            "description": "",
            "created_by": ctx.author.id,
            "updated_by": ctx.author.id,
            "created_at": discord.utils.utcnow().isoformat(),
            "updated_at": discord.utils.utcnow().isoformat(),
        }
        await self._save_alias(payload)
        self.aliases[name] = payload
        self._register_dynamic_command(name)

        await ctx.send(
            f"Created argument alias `{name}` in `{mode}` mode. Run it with `{ctx.prefix}{name} ...`."
        )

    @checks.has_permissions(MOD_LEVEL)
    @argaliases.command(name="edit", aliases=["update"])
    async def argaliases_edit(self, ctx: commands.Context, name: str, *, raw: str):
        """Edit an existing argument alias.

        Syntax:
        ?argaliases edit <name> [mode] <template>
        """
        name = name.lower().strip()
        alias = self.aliases.get(name)
        if not alias:
            return await ctx.send(f"`{name}` does not exist.")

        mode, template = self._parse_template_input(raw)
        if not template:
            return await ctx.send("Please provide a replacement template.")

        alias["mode"] = mode
        alias["template"] = template
        alias["updated_by"] = ctx.author.id
        alias["updated_at"] = discord.utils.utcnow().isoformat()

        await self._save_alias(alias)
        self.aliases[name] = alias
        self._register_dynamic_command(name)

        await ctx.send(f"Updated `{name}`. It will now run in `{mode}` mode.")

    @checks.has_permissions(MOD_LEVEL)
    @argaliases.command(name="describe", aliases=["description", "desc"])
    async def argaliases_describe(self, ctx: commands.Context, name: str, *, description: str):
        """Set or update an alias description."""
        name = name.lower().strip()
        alias = self.aliases.get(name)
        if not alias:
            return await ctx.send(f"`{name}` does not exist.")

        alias["description"] = description.strip()
        alias["updated_by"] = ctx.author.id
        alias["updated_at"] = discord.utils.utcnow().isoformat()

        await self._save_alias(alias)
        self.aliases[name] = alias
        self._register_dynamic_command(name)

        await ctx.send(f"Updated the description for `{name}`.")

    @checks.has_permissions(MOD_LEVEL)
    @argaliases.command(name="delete", aliases=["remove", "del"])
    async def argaliases_delete(self, ctx: commands.Context, name: str):
        """Delete an argument alias."""
        name = name.lower().strip()
        if name not in self.aliases:
            return await ctx.send(f"`{name}` does not exist.")

        await self._delete_alias(name)
        self.aliases.pop(name, None)
        self._unregister_dynamic_command(name)
        await ctx.send(f"Deleted argument alias `{name}`.")

    @checks.has_permissions(MOD_LEVEL)
    @argaliases.command(name="show", aliases=["view", "info"])
    async def argaliases_show(self, ctx: commands.Context, name: str):
        """Show the current configuration for an alias."""
        name = name.lower().strip()
        alias = self.aliases.get(name)
        if not alias:
            return await ctx.send(f"`{name}` does not exist.")

        await ctx.send(embed=self._alias_embed(alias))

    @checks.has_permissions(MOD_LEVEL)
    @argaliases.command(name="list")
    async def argaliases_list(self, ctx: commands.Context):
        """List all argument aliases."""
        if not self.aliases:
            return await ctx.send("There are no argument aliases yet.")

        lines = []
        for name in sorted(self.aliases):
            alias = self.aliases[name]
            desc = alias.get("description") or "No description"
            lines.append(f"• `{name}` — `{alias.get('mode', 'reply')}` — {desc[:60]}")

        embed = discord.Embed(
            title="Argument aliases",
            description="\n".join(lines[:25]),
            colour=getattr(self.bot, "main_color", discord.Colour.blurple()),
        )
        if len(lines) > 25:
            embed.set_footer(text=f"Showing 25 of {len(lines)} aliases.")
        await ctx.send(embed=embed)

    @checks.has_permissions(MOD_LEVEL)
    @argaliases.command(name="test", aliases=["preview"])
    async def argaliases_test(self, ctx: commands.Context, name: str, *, raw_args: str = ""):
        """Preview the rendered output without sending it to the recipient."""
        name = name.lower().strip()
        alias = self.aliases.get(name)
        if not alias:
            return await ctx.send(f"`{name}` does not exist.")

        rendered = self._render_template(alias.get("template", ""), ctx, raw_args.strip())
        embed = discord.Embed(
            title=f"Preview: {name}",
            description=f"```\n{rendered[:3500]}\n```" if rendered else "*(empty output)*",
            colour=getattr(self.bot, "main_color", discord.Colour.blurple()),
        )
        embed.add_field(name="Mode", value=alias.get("mode", "reply"), inline=True)
        embed.add_field(name="Arguments", value=raw_args or "(none)", inline=False)
        await ctx.send(embed=embed)

    @checks.has_permissions(MOD_LEVEL)
    @argaliases.command(name="docs", aliases=["helpvars", "placeholders"])
    async def argaliases_docs(self, ctx: commands.Context):
        """Show inline docs for placeholder usage."""
        prefix = ctx.prefix
        embed = discord.Embed(
            title="Argument alias docs",
            colour=getattr(self.bot, "main_color", discord.Colour.blurple()),
            description=(
                "Create reusable commands that accept extra text after the command name.\n\n"
                f"`{prefix}argaliases create deny reply Your application has been denied.{'{newline}'}Reason: {{args}}`\n"
                f"`{prefix}argaliases create approve reply Your application has been approved.{'{newline}'}Notes: {{args}}`\n"
                f"Run them with `{prefix}deny ...` or `{prefix}approve ...`."
            ),
        )
        embed.add_field(
            name="Modes",
            value=(
                "`reply` = normal Modmail reply\n"
                "`anonreply` = anonymous Modmail reply\n"
                "`plainreply` = plain reply\n"
                "`note` = send only in the staff thread channel"
            ),
            inline=False,
        )
        embed.add_field(
            name="Placeholders",
            value=(
                "`{args}` = full argument text\n"
                "`{1}` / `{2}` = individual words\n"
                "`{1+}` / `{2+}` = word onward\n"
                "`{author}` = staff display name\n"
                "`{recipient}` = first recipient display name\n"
                "`{newline}` = line break"
            ),
            inline=False,
        )
        embed.set_footer(text="Use ?argaliases show <name> to inspect one alias, or ?argaliases test <name> <args> to preview it.")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(ArgAliases(bot))
