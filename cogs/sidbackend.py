"""Registration, whitelist, and tagging commands for the image bot."""

import discord
from discord.ext import commands

from .imagestorage import clean_name


class SidBackend(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="register")
    @commands.is_owner()
    async def register(self, ctx: commands.Context):
        """Provision the current guild's image storage. Bot owner only."""
        store = self.bot.storage.guild(ctx.guild.id)
        icon_url = str(ctx.guild.icon.url) if ctx.guild.icon else None
        if await store.register(ctx.guild.name, icon_url):
            await ctx.send("done twin")
        else:
            await store.update_guild_profile(ctx.guild.name, icon_url)
            await ctx.send("this server is already registered twin")

    @commands.command(name="whitelist", aliases=["wl"])
    async def whitelist(
        self,
        ctx: commands.Context,
        action: str | None = None,
        user: discord.Member | None = None,
    ):
        """Add or remove a member permitted to delete saved images."""
        is_bot_owner = await self.bot.is_owner(ctx.author)
        if not is_bot_owner and not ctx.author.guild_permissions.administrator:
            await ctx.send("only a server admin or the bot owner can do that twin")
            return

        store = self.bot.storage.guild(ctx.guild.id)
        if not store.is_registered():
            await ctx.send("register this server first twin")
            return
        if action is None or user is None:
            await ctx.send("use `!whitelist add @user` or `!whitelist remove @user` twin")
            return

        action = action.lower()
        if action == "add":
            if await store.whitelist_add(user.id):
                await ctx.send(f"`{user.display_name}` can delete images now twin")
            else:
                await ctx.send(f"`{user.display_name}` is already whitelisted twin")
        elif action in {"remove", "delete"}:
            if await store.whitelist_remove(user.id):
                await ctx.send(f"`{user.display_name}` can't delete images anymore twin")
            else:
                await ctx.send(f"`{user.display_name}` wasn't whitelisted twin")
        else:
            await ctx.send("use `add` or `remove` twin")

    @commands.command(name="tag")
    async def tag(self, ctx: commands.Context, name: str | None = None, *, tags: str | None = None):
        """Append comma-separated tags to an image without replacing old tags."""
        store = self.bot.storage.guild(ctx.guild.id)
        if not store.is_registered():
            await ctx.send("this server isn't registered yet twin")
            return

        clean = clean_name(name)
        if clean is None:
            await ctx.send("give me the image name first twin")
            return
        if not tags or not tags.strip():
            await ctx.send("add at least one tag after the image name twin")
            return

        result = await store.add_tags(clean, tags.split(","))
        if result is None:
            await ctx.send(f"i don't have an image called `{clean}` twin")
            return

        added, skipped = result
        if added:
            response = f"added {', '.join(f'`{tag}`' for tag in added)} to `{clean}` twin"
            if skipped:
                response += f" — already had {', '.join(f'`{tag}`' for tag in skipped)}"
            await ctx.send(response)
        else:
            await ctx.send(f"`{clean}` already has those tag(s) twin")


async def setup(bot: commands.Bot):
    await bot.add_cog(SidBackend(bot))
