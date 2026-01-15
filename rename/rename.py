import re
import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel

import datetime

def clean_channel_name(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9 -]", "", text)
    text = text.replace(" ", "-")
    text = re.sub(r"-{2,}", "-", text).strip("-")
    
    return text[:90] if text else "ticket"
class Rename(commands.Cog):
    """Rename a thread!"""

    def __init__(self, bot):
        self.bot = bot

    @checks.thread_only()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command()
    async def rename(self, ctx, *, request: str = None):
        try:
            await ctx.message.add_reaction("⏰")

            opener = ctx.thread.recipient
            user_part = clean_channel_name(getattr(opener, "name", "user"))

            if request:
                req_part = clean_channel_name(request)
                new_name = clean_channel_name(f"{req_part}-{user_part}")
            else:
                new_name = clean_channel_name(f"ticket-{user_part}")

            await ctx.channel.edit(name=new_name)

            await ctx.message.clear_reactions()
            await ctx.message.add_reaction("✅")

        except discord.errors.Forbidden:
            embed = discord.Embed(
                title="Forbidden",
                description="Uh oh, it seems I can't perform this action due to my permission levels.",
                color=discord.Color.red(),
            )
            embed.timestamp = datetime.datetime.utcnow()
            embed.set_footer(text="Rename")

            await ctx.reply(embed=embed)

            await ctx.message.clear_reactions()
            await ctx.message.add_reaction("❌")

        except Exception:
            await ctx.message.clear_reactions()
            await ctx.message.add_reaction("❌")

async def setup(bot):
    await bot.add_cog(Rename(bot))
