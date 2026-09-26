"""
Sticky Panel - Modmail plugin

Original code from: https://github.com/Ollieg3/Modmail-Plugins
This is an improved version that works with any Modmail bot, whatever prefix it uses.
"""

import asyncio
import copy
import json
import os

import discord
from discord.ext import commands

CONFIG_PATH = "sticky_panel_config.json"

# The footer doubles as a marker so the bot can recognise its own panels
PANEL_FOOTER = "Support Centre Panel"
PANEL_FOOTER_TEXT = (
    f"{PANEL_FOOTER} • You can still use normal alias commands, "
    "these are just common buttons to make it faster for yourselves."
)

DEFAULT_TITLE = "Ticket Control Panel"
DEFAULT_DESCRIPTION = "Select an action below to manage this ticket."
DEFAULT_DELAY = 5.0

DEFAULT_CONFIG = {
    "enabled": False,
    "delay": DEFAULT_DELAY,
    "title": DEFAULT_TITLE,
    "description": DEFAULT_DESCRIPTION,
    "color": 0x5865F2,
    "categories": [],
    "buttons": []
}

STYLE_MAP = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
    "blurple": discord.ButtonStyle.primary,
    "blue": discord.ButtonStyle.primary,
    "grey": discord.ButtonStyle.secondary,
    "gray": discord.ButtonStyle.secondary,
    "green": discord.ButtonStyle.success,
    "red": discord.ButtonStyle.danger,
}

MAX_ROWS = 4          # buttons use rows 1-4, row 0 is the category dropdown
MAX_PER_ROW = 5       # Discord limit
MAX_SELECT_OPTIONS = 25


# --- SAFE JSON HANDLING ---
def save_config(config):
    tmp_path = f"{CONFIG_PATH}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception as e:
        print(f"[StickyPanel] Error saving config: {e}")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        config = copy.deepcopy(DEFAULT_CONFIG)
        save_config(config)
        return config

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config is not a JSON object")
    except Exception as e:
        # Keep the broken file instead of silently overwriting it
        backup = f"{CONFIG_PATH}.broken"
        print(f"[StickyPanel] Config load failed ({e}). Backed up to {backup} and reverted to default.")
        try:
            os.replace(CONFIG_PATH, backup)
        except OSError:
            pass
        config = copy.deepcopy(DEFAULT_CONFIG)
        save_config(config)
        return config

    for key, value in DEFAULT_CONFIG.items():
        data.setdefault(key, copy.deepcopy(value))
    return data


# --- HELPERS ---
def truncate(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clamp_row(value):
    try:
        row = int(value)
    except (TypeError, ValueError):
        row = 1
    return min(max(row, 1), MAX_ROWS)


def strip_prefix(text, prefix):
    text = text.strip()
    for p in {prefix, "-"}:
        while p and text.startswith(p):
            text = text[len(p):].strip()
    return text


def buttons_for_scope(buttons, category_id):
    """Buttons that show up in a channel under `category_id` (universal + locked to that category)."""
    return [b for b in buttons if not b.get("category_id") or b.get("category_id") == category_id]


def build_category_options(categories):
    options, mapping = [], {}
    for cat in categories:
        value = str(cat.get("value", "")).strip()
        if not value or value in mapping or len(options) >= MAX_SELECT_OPTIONS:
            continue
        mapping[value] = cat
        desc = cat.get("description")
        options.append(discord.SelectOption(
            label=truncate(cat.get("label") or value, 100),
            value=value,
            description=truncate(desc, 100) if desc else None,
            emoji=cat.get("emoji") or None
        ))
    return options, mapping


# --- MODALS FOR CONFIGURATION ---
class AddButtonModal(discord.ui.Modal, title="➕ Add / Edit Action Button"):
    label_input = discord.ui.TextInput(
        label="Button Label",
        placeholder="e.g. Refund",
        max_length=80,
        required=True
    )
    alias_input = discord.ui.TextInput(
        label="Command Alias (without prefix)",
        placeholder="e.g. refund",
        max_length=50,
        required=True
    )
    category_input = discord.ui.TextInput(
        label="Category ID (Optional)",
        placeholder="e.g. 123456789012345678",
        max_length=100,
        required=False
    )
    style_input = discord.ui.TextInput(
        label="Color Style (blue, grey, green, red)",
        placeholder="blue",
        default="blue",
        max_length=20,
        required=False
    )
    row_input = discord.ui.TextInput(
        label="Row (1-4)",
        placeholder="1",
        default="1",
        max_length=1,
        required=False
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        cog = self.cog
        buttons = cog.config["buttons"]

        label_val = self.label_input.value.strip()
        if not label_val:
            return await interaction.response.send_message("❌ Button label can't be empty.", ephemeral=True)

        alias_val = strip_prefix(self.alias_input.value, cog.prefix)
        if not alias_val:
            return await interaction.response.send_message("❌ Alias can't be empty.", ephemeral=True)

        cat_target = self.category_input.value.strip() or None
        if cat_target and not cat_target.isdigit():
            return await interaction.response.send_message("❌ Category ID must be a number (right click the category → Copy ID).", ephemeral=True)

        style_val = self.style_input.value.strip().lower() or "blue"
        if style_val not in STYLE_MAP:
            return await interaction.response.send_message("❌ Invalid color style! Use: `blue`, `grey`, `green`, or `red`.", ephemeral=True)

        row_raw = self.row_input.value.strip() or "1"
        if not row_raw.isdigit():
            return await interaction.response.send_message("❌ Row must be a number between 1 and 4.", ephemeral=True)
        row_val = clamp_row(row_raw)

        existing = next((b for b in buttons if b["label"].lower() == label_val.lower()), None)
        others = [b for b in buttons if b is not existing]

        # A universal button shares rows with every category's buttons, so check the busiest one
        if cat_target:
            scopes = {cat_target}
        else:
            scopes = {None} | {b.get("category_id") for b in others if b.get("category_id")}
        busiest = max(
            sum(1 for b in buttons_for_scope(others, scope) if clamp_row(b.get("row", 1)) == row_val)
            for scope in scopes
        )
        if busiest >= MAX_PER_ROW:
            return await interaction.response.send_message(
                f"❌ Row `{row_val}` is already full (5 buttons) for this scope. Choose a different row.",
                ephemeral=True
            )

        if existing:
            existing.update({
                "label": label_val,
                "alias": alias_val,
                "style": style_val,
                "row": row_val,
                "category_id": cat_target
            })
            action_type = "Updated"
        else:
            buttons.append({
                "label": label_val,
                "alias": alias_val,
                "style": style_val,
                "row": row_val,
                "category_id": cat_target
            })
            action_type = "Added"

        save_config(cog.config)

        lock_text = f"`{cat_target}`" if cat_target else "*Universal (All categories)*"
        await interaction.response.send_message(
            f"✅ Successfully **{action_type}** button!\n"
            f"• **Label:** {label_val}\n"
            f"• **Runs Command:** `{cog.prefix}{alias_val}`\n"
            f"• **Row:** {row_val}\n"
            f"• **Category Lock:** {lock_text}"
        )


class AddCategoryModal(discord.ui.Modal, title="📁 Add / Edit Category Option"):
    label_input = discord.ui.TextInput(
        label="Dropdown Label",
        placeholder="e.g. Billing",
        max_length=100,
        required=True
    )
    value_input = discord.ui.TextInput(
        label="Target Category ID",
        placeholder="e.g. 123456789012345678",
        max_length=100,
        required=True
    )
    emoji_input = discord.ui.TextInput(
        label="Emoji (Optional)",
        placeholder="e.g. 💳",
        max_length=10,
        required=False
    )
    alias_input = discord.ui.TextInput(
        label="Auto-run Alias/Snippet",
        placeholder="e.g. billing_snippet",
        max_length=50,
        required=False
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        cog = self.cog
        categories = cog.config["categories"]

        label_val = self.label_input.value.strip()
        value_val = self.value_input.value.strip()
        emoji_val = self.emoji_input.value.strip() or None
        alias_val = strip_prefix(self.alias_input.value, cog.prefix) or None

        if not label_val:
            return await interaction.response.send_message("❌ Dropdown label can't be empty.", ephemeral=True)
        if not value_val.isdigit():
            return await interaction.response.send_message("❌ Category ID must be a number (right click the category → Copy ID).", ephemeral=True)

        existing = next((c for c in categories if c["label"].lower() == label_val.lower()), None)

        duplicate = next((c for c in categories if c is not existing and str(c.get("value")) == value_val), None)
        if duplicate:
            return await interaction.response.send_message(
                f"❌ That category ID is already used by **{duplicate['label']}**.", ephemeral=True
            )
        if not existing and len(categories) >= MAX_SELECT_OPTIONS:
            return await interaction.response.send_message("❌ You can only have 25 dropdown categories.", ephemeral=True)

        data = {
            "label": label_val,
            "value": value_val,
            "description": f"Move to {label_val}",
            "emoji": emoji_val,
            "alias": alias_val
        }
        if existing:
            existing.update(data)
            action_type = "Updated"
        else:
            categories.append(data)
            action_type = "Added"

        save_config(cog.config)

        await interaction.response.send_message(
            f"✅ Successfully **{action_type}** category option!\n"
            f"• **Dropdown Label:** {emoji_val or ''} {label_val}\n"
            f"• **Target Category ID:** `{value_val}`\n"
            f"• **Auto-run:** " + (f"`{cog.prefix}{alias_val}`" if alias_val else "*None*")
        )


# --- INTERACTIVE REMOVAL DROPDOWNS ---
class RemoveButtonSelect(discord.ui.Select):
    def __init__(self, cog):
        self.cog = cog
        options = []
        for btn in cog.config["buttons"][:MAX_SELECT_OPTIONS]:
            scope = f"Cat: {btn['category_id']}" if btn.get("category_id") else "Universal"
            options.append(discord.SelectOption(
                label=truncate(btn["label"], 100),
                value=btn["label"],
                description=truncate(f"Runs {cog.prefix}{btn['alias']} • {scope}", 100)
            ))
        super().__init__(placeholder="Select a button to remove...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_label = self.values[0]
        self.cog.config["buttons"] = [b for b in self.cog.config["buttons"] if b["label"] != selected_label]
        save_config(self.cog.config)
        await interaction.response.edit_message(
            content=f"🗑️ Successfully removed panel button: **{selected_label}**", view=None
        )


class RemoveButtonView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=60)
        self.add_item(RemoveButtonSelect(cog))


class RemoveCategorySelect(discord.ui.Select):
    def __init__(self, cog):
        self.cog = cog
        options = []
        for cat in cog.config["categories"][:MAX_SELECT_OPTIONS]:
            options.append(discord.SelectOption(
                label=truncate(cat["label"], 100),
                value=cat["label"],
                description=truncate(f"ID: {cat['value']}", 100)
            ))
        super().__init__(placeholder="Select a category to remove...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_label = self.values[0]
        self.cog.config["categories"] = [c for c in self.cog.config["categories"] if c["label"] != selected_label]
        save_config(self.cog.config)
        await interaction.response.edit_message(
            content=f"🗑️ Successfully removed dropdown category: **{selected_label}**", view=None
        )


class RemoveCategoryView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=60)
        self.add_item(RemoveCategorySelect(cog))


# --- PANEL BUTTON ---
class PanelButton(discord.ui.Button):
    def __init__(self, cog, data: dict, row: int):
        super().__init__(
            label=truncate(data["label"], 80),
            style=STYLE_MAP.get(str(data.get("style", "blue")).lower(), discord.ButtonStyle.primary),
            emoji=data.get("emoji") or None,
            row=row
        )
        self.cog = cog
        self.alias = data["alias"]

    async def callback(self, interaction: discord.Interaction):
        if not await self.cog.is_modmail_thread(interaction.channel):
            return await interaction.response.send_message("This is not an active Modmail thread.", ephemeral=True)

        # Silent acknowledge; the command's own output is the feedback
        await interaction.response.defer()
        try:
            await self.cog.run_alias(interaction, self.alias)
        except Exception as e:
            print(f"[StickyPanel] Error running '{self.alias}': {e}")
            await interaction.followup.send(f"❌ Couldn't run `{self.cog.prefix}{self.alias}`: {e}", ephemeral=True)


# --- CATEGORY DROPDOWN ---
class CategorySelect(discord.ui.Select):
    def __init__(self, cog, options, mapping):
        self.cog = cog
        self.mapping = mapping
        super().__init__(
            placeholder="📁 Move thread to category...",
            min_values=1,
            max_values=1,
            options=options,
            row=0
        )

    async def callback(self, interaction: discord.Interaction):
        if not await self.cog.is_modmail_thread(interaction.channel):
            return await interaction.response.send_message("This is not an active Modmail thread.", ephemeral=True)

        selected_value = self.values[0]
        alias_to_run = self.mapping.get(selected_value, {}).get("alias")

        await interaction.response.defer()
        try:
            await self.cog.run_alias(interaction, f"move {selected_value}")
            if alias_to_run:
                await asyncio.sleep(0.5)
                await self.cog.run_alias(interaction, alias_to_run)
        except Exception as e:
            print(f"[StickyPanel] Category select error: {e}")
            await interaction.followup.send(f"❌ Something went wrong moving the thread: {e}", ephemeral=True)

        # Resend so the category-locked buttons match the new category
        self.cog.schedule_resend(interaction.channel)


# --- MAIN PANEL VIEW ---
class StickyPanelView(discord.ui.View):
    def __init__(self, cog, channel):
        super().__init__(timeout=None)
        config = cog.config

        options, mapping = build_category_options(config.get("categories", []))
        if options:
            self.add_item(CategorySelect(cog, options, mapping))

        current_cat_id = None
        if isinstance(channel, discord.TextChannel) and channel.category:
            current_cat_id = str(channel.category.id)

        # Place each button on its chosen row, spilling over to the next free row if it's full
        row_counts = {r: 0 for r in range(1, MAX_ROWS + 1)}
        for btn in buttons_for_scope(config.get("buttons", []), current_cat_id):
            if not btn.get("label") or not btn.get("alias"):
                continue
            wanted = clamp_row(btn.get("row", 1))
            search_order = list(range(wanted, MAX_ROWS + 1)) + list(range(1, wanted))
            row = next((r for r in search_order if row_counts[r] < MAX_PER_ROW), None)
            if row is None:
                print(f"[StickyPanel] No room left for button '{btn['label']}', skipped.")
                continue
            try:
                self.add_item(PanelButton(cog, btn, row))
                row_counts[row] += 1
            except Exception as e:
                print(f"[StickyPanel] Skipped invalid button '{btn.get('label')}': {e}")


# --- COG & LISTENERS ---
class StickyPanel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = load_config()
        self.sticky_messages = {}   # channel_id -> panel message id
        self.pending = {}           # channel_id -> scheduled resend task
        self.locks = {}

    def cog_unload(self):
        for task in self.pending.values():
            task.cancel()

    @property
    def prefix(self):
        prefix = getattr(self.bot, "prefix", None)
        return prefix if isinstance(prefix, str) and prefix else "-"

    def get_lock(self, channel_id):
        if channel_id not in self.locks:
            self.locks[channel_id] = asyncio.Lock()
        return self.locks[channel_id]

    async def is_modmail_thread(self, channel):
        if not isinstance(channel, discord.TextChannel) or not hasattr(self.bot, "threads"):
            return False
        try:
            return await self.bot.threads.find(channel=channel) is not None
        except Exception:
            return False

    def is_panel_message(self, message):
        if not self.bot.user or message.author.id != self.bot.user.id:
            return False
        return any(e.footer and e.footer.text and e.footer.text.startswith(PANEL_FOOTER) for e in message.embeds)

    async def run_alias(self, interaction, alias):
        """Runs a command/alias/snippet as the person who clicked, through Modmail's normal pipeline."""
        message = copy.copy(interaction.message)  # don't touch the cached panel message
        message.content = f"{self.prefix}{alias}"
        message.author = interaction.user
        await self.bot.process_commands(message)

    def build_panel_embed(self):
        color_val = self.config.get("color", 0x5865F2)
        if isinstance(color_val, str):
            try:
                color_val = int(color_val.lstrip("#"), 16)
            except ValueError:
                color_val = 0x5865F2

        embed = discord.Embed(
            title=self.config.get("title") or DEFAULT_TITLE,
            description=self.config.get("description") or DEFAULT_DESCRIPTION,
            color=color_val
        )
        embed.set_footer(text=PANEL_FOOTER_TEXT)
        return embed

    # --- RESENDING ---
    def schedule_resend(self, channel, delay=None):
        """Debounced: a burst of messages only resends the panel once, after things go quiet."""
        if not self.config.get("enabled", False) or not isinstance(channel, discord.TextChannel):
            return

        existing = self.pending.get(channel.id)
        if existing and not existing.done():
            existing.cancel()

        if delay is None:
            try:
                delay = float(self.config.get("delay", DEFAULT_DELAY))
            except (TypeError, ValueError):
                delay = DEFAULT_DELAY

        self.pending[channel.id] = asyncio.create_task(self._resend_after(channel, delay))

    async def _resend_after(self, channel, delay):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        # From here on this task can't be cancelled by a newer message
        if self.pending.get(channel.id) is asyncio.current_task():
            self.pending.pop(channel.id, None)

        async with self.get_lock(channel.id):
            try:
                await self._post_panel(channel)
            except Exception as e:
                print(f"[StickyPanel] Failed to resend panel in {channel.id}: {e}")

    async def resend_sticky(self, channel):
        self.schedule_resend(channel, delay=0)

    async def _clear_old_panels(self, channel):
        """After a restart we don't know the old panel's ID, so find and remove any leftover panels."""
        try:
            async for msg in channel.history(limit=30):
                if self.is_panel_message(msg):
                    try:
                        await msg.delete()
                    except discord.HTTPException:
                        pass
        except discord.HTTPException:
            pass

    async def _post_panel(self, channel):
        old_msg_id = self.sticky_messages.pop(channel.id, None)
        if old_msg_id:
            try:
                await channel.get_partial_message(old_msg_id).delete()
            except discord.HTTPException:
                pass
        else:
            await self._clear_old_panels(channel)

        try:
            new_msg = await channel.send(embed=self.build_panel_embed(), view=StickyPanelView(self, channel))
            self.sticky_messages[channel.id] = new_msg.id
        except discord.HTTPException as e:
            print(f"[StickyPanel] Failed to send panel in {channel.id} (check button/category emojis are valid): {e}")

    # --- COMMANDS ---
    @commands.group(name="stickypanel", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def stickypanel_cmd(self, ctx):
        embed = discord.Embed(title="⚙️ Sticky Panel Settings", color=discord.Color.blue())
        embed.add_field(name="Status", value="🟢 Enabled" if self.config.get("enabled") else "🔴 Disabled", inline=True)
        embed.add_field(name="Delay", value=f"`{self.config.get('delay')}s`", inline=True)
        embed.add_field(name="Title", value=truncate(self.config.get("title") or "-", 1024), inline=False)

        btns = self.config.get("buttons", [])
        btns_text = "\n".join(
            f"• **{b['label']}** (`{self.prefix}{b['alias']}`) row {clamp_row(b.get('row', 1))}"
            + (f" [Cat: `{b['category_id']}`]" if b.get("category_id") else " [Universal]")
            for b in btns
        ) if btns else "*None configured*"
        embed.add_field(name=f"Buttons ({len(btns)})", value=truncate(btns_text, 1024), inline=False)

        cats = self.config.get("categories", [])
        cats_text = "\n".join(
            f"• {c.get('emoji') or ''} **{c['label']}** → `{c['value']}`"
            + (f" (runs `{self.prefix}{c['alias']}`)" if c.get("alias") else "")
            for c in cats
        ) if cats else "*None configured*"
        embed.add_field(name=f"Categories ({len(cats)})", value=truncate(cats_text, 1024), inline=False)

        p = f"{self.prefix}stickypanel"
        embed.add_field(
            name="Commands",
            value=(
                f"`{p} enable` / `{p} disable`\n"
                f"`{p} preview` • `{p} refresh`\n"
                f"`{p} addbutton` / `{p} removebutton`\n"
                f"`{p} addcategory` / `{p} removecategory`\n"
                f"`{p} title <text>` • `{p} description <text>`\n"
                f"`{p} delay <seconds>` • `{p} color <#hex>`\n"
                f"`{p} reset` (restore default title/description/delay/color)"
            ),
            inline=False
        )

        await ctx.send(embed=embed)

    @stickypanel_cmd.command(name="enable")
    @commands.has_permissions(administrator=True)
    async def enable_panel(self, ctx):
        self.config["enabled"] = True
        save_config(self.config)
        await ctx.send("✅ **Sticky Panel enabled!**")

    @stickypanel_cmd.command(name="disable")
    @commands.has_permissions(administrator=True)
    async def disable_panel(self, ctx):
        self.config["enabled"] = False
        save_config(self.config)
        for task in self.pending.values():
            task.cancel()
        self.pending.clear()
        await ctx.send("🛑 **Sticky Panel disabled.**")

    @stickypanel_cmd.command(name="preview")
    @commands.has_permissions(administrator=True)
    async def panel_preview(self, ctx):
        await ctx.send("🔍 **Panel Preview:**", embed=self.build_panel_embed(), view=StickyPanelView(self, ctx.channel))

    @stickypanel_cmd.command(name="refresh")
    @commands.has_permissions(administrator=True)
    async def refresh_panel(self, ctx):
        """Resend the panel in this ticket straight away (e.g. after changing buttons)."""
        if not self.config.get("enabled", False):
            return await ctx.send(f"❌ The panel is disabled. Use `{self.prefix}stickypanel enable` first.")
        if not await self.is_modmail_thread(ctx.channel):
            return await ctx.send("❌ Run this inside a Modmail ticket.")
        self.schedule_resend(ctx.channel, delay=0)
        try:
            await ctx.message.add_reaction("✅")
        except discord.HTTPException:
            pass

    @stickypanel_cmd.command(name="title")
    @commands.has_permissions(administrator=True)
    async def set_title(self, ctx, *, text: str):
        text = text.strip()
        if len(text) > 256:
            return await ctx.send("❌ Title can be 256 characters max.")
        self.config["title"] = text
        save_config(self.config)
        await ctx.send(f"✅ Panel title set to **{text}**")

    @stickypanel_cmd.command(name="description")
    @commands.has_permissions(administrator=True)
    async def set_description(self, ctx, *, text: str):
        text = text.strip()
        if len(text) > 4000:
            return await ctx.send("❌ Description can be 4000 characters max.")
        self.config["description"] = text
        save_config(self.config)
        await ctx.send("✅ Panel description updated.")

    @stickypanel_cmd.command(name="delay")
    @commands.has_permissions(administrator=True)
    async def set_delay(self, ctx, seconds: float):
        if not 0 <= seconds <= 60:
            return await ctx.send("❌ Delay must be between 0 and 60 seconds.")
        self.config["delay"] = seconds
        save_config(self.config)
        await ctx.send(f"✅ Panel delay set to `{seconds:g}s`")

    @stickypanel_cmd.command(name="color", aliases=["colour"])
    @commands.has_permissions(administrator=True)
    async def set_color(self, ctx, hex_code: str):
        try:
            value = int(hex_code.strip().lstrip("#"), 16)
            if not 0 <= value <= 0xFFFFFF:
                raise ValueError
        except ValueError:
            return await ctx.send("❌ Use a hex colour like `#5865F2`.")
        self.config["color"] = value
        save_config(self.config)
        await ctx.send(embed=discord.Embed(description=f"✅ Panel colour set to `#{value:06X}`", color=value))

    @stickypanel_cmd.command(name="reset")
    @commands.has_permissions(administrator=True)
    async def reset_appearance(self, ctx):
        """Restores title, description, delay and colour to defaults. Buttons and categories are kept."""
        for key in ("title", "description", "delay", "color"):
            self.config[key] = DEFAULT_CONFIG[key]
        save_config(self.config)
        await ctx.send(
            f"♻️ Reset to defaults: title **{DEFAULT_TITLE}**, delay `{DEFAULT_DELAY:g}s`. "
            "Your buttons and categories were kept."
        )

    @stickypanel_cmd.command(name="addbutton")
    @commands.has_permissions(administrator=True)
    async def add_button(self, ctx):
        button = discord.ui.Button(label="Open Button Form 📝", style=discord.ButtonStyle.primary)

        async def btn_callback(interaction: discord.Interaction):
            await interaction.response.send_modal(AddButtonModal(self))

        button.callback = btn_callback
        view = discord.ui.View()
        view.add_item(button)
        await ctx.send("Click below to configure the action button:", view=view)

    @stickypanel_cmd.command(name="removebutton")
    @commands.has_permissions(administrator=True)
    async def remove_button(self, ctx):
        if not self.config["buttons"]:
            return await ctx.send("❌ There are no custom buttons configured to remove.")
        await ctx.send("Select the button you want to remove from the dropdown below:", view=RemoveButtonView(self))

    @stickypanel_cmd.command(name="addcategory")
    @commands.has_permissions(administrator=True)
    async def add_category(self, ctx):
        button = discord.ui.Button(label="Open Category Form 📁", style=discord.ButtonStyle.primary)

        async def btn_callback(interaction: discord.Interaction):
            await interaction.response.send_modal(AddCategoryModal(self))

        button.callback = btn_callback
        view = discord.ui.View()
        view.add_item(button)
        await ctx.send("Click below to configure the category option:", view=view)

    @stickypanel_cmd.command(name="removecategory")
    @commands.has_permissions(administrator=True)
    async def remove_category(self, ctx):
        if not self.config["categories"]:
            return await ctx.send("❌ There are no categories configured to remove.")
        await ctx.send("Select the category you want to remove from the dropdown below:", view=RemoveCategoryView(self))

    # --- LISTENERS ---
    @commands.Cog.listener()
    async def on_thread_ready(self, thread, *args, **kwargs):
        channel = getattr(thread, "channel", None)
        if channel:
            self.schedule_resend(channel, delay=2.0)

    @commands.Cog.listener()
    async def on_thread_close(self, thread, *args, **kwargs):
        channel = getattr(thread, "channel", None)
        if not channel:
            return
        task = self.pending.pop(channel.id, None)
        if task:
            task.cancel()
        self.sticky_messages.pop(channel.id, None)
        self.locks.pop(channel.id, None)

    @commands.Cog.listener()
    async def on_message(self, message):
        if not self.config.get("enabled", False):
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        # Only bot messages (Modmail relays + replies) move the panel down
        if not message.author.bot:
            return
        if self.is_panel_message(message):
            return
        if not await self.is_modmail_thread(message.channel):
            return

        self.schedule_resend(message.channel)


async def setup(bot):
    await bot.add_cog(StickyPanel(bot))