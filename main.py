import os
import traceback
import json
import time
from pathlib import Path
from uuid import uuid4

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from cogs.imagestorage import Storage

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ROOT = Path(__file__).parent
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", ROOT / "storage"))
COGS_DIR = ROOT / "cogs"

# create folders right away, before the bot even tries to log in
COGS_DIR.mkdir(exist_ok=True)
(COGS_DIR / "__init__.py").touch(exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)


class ImageBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            case_insensitive=True,
        )

        # cogs read this instead of hardcoding paths
        self.storage_dir = STORAGE_DIR
        self.storage = Storage(STORAGE_DIR)
        self.active_guilds_file = STORAGE_DIR / "active_guilds.json"

    def write_active_guilds(self) -> None:
        """Atomically publish a non-secret, local bot-presence heartbeat."""
        payload = {"guild_ids": sorted(str(guild.id) for guild in self.guilds), "updated_at": time.time()}
        temporary = self.active_guilds_file.with_name(f".{self.active_guilds_file.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(self.active_guilds_file)

    @tasks.loop(seconds=30)
    async def active_guild_heartbeat(self) -> None:
        self.write_active_guilds()

    @active_guild_heartbeat.before_loop
    async def before_active_guild_heartbeat(self) -> None:
        await self.wait_until_ready()

    async def setup_hook(self):
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        COGS_DIR.mkdir(exist_ok=True)  # create the cogs folder if it's missing

        # auto-load every cog in /cogs (skips files starting with _)
        loaded = 0
        for file in sorted(COGS_DIR.glob("*.py")):
            # imagestorage is shared disk-access code, not a Discord extension.
            if file.name.startswith("_") or file.stem == "imagestorage":
                continue
            await self.load_extension(f"cogs.{file.stem}")
            print(f"loaded cogs.{file.stem}")
            loaded += 1

        if loaded == 0:
            print("no cogs found in /cogs, the bot will start but have no commands")

    async def on_ready(self):
        # Keep the local gallery's display names/icons current. This does not
        # create storage for unregistered guilds.
        for guild in self.guilds:
            store = self.storage.guild(guild.id)
            if store.is_registered():
                icon_url = str(guild.icon.url) if guild.icon else None
                await store.update_guild_profile(guild.name, icon_url)
        self.write_active_guilds()
        if not self.active_guild_heartbeat.is_running():
            self.active_guild_heartbeat.start()
        print(f"logged in as {self.user} | in {len(self.guilds)} server(s)")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        self.write_active_guilds()

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.write_active_guilds()

    async def on_command_error(self, ctx: commands.Context, error: Exception):
        # let commands/cogs with their own error handlers deal with it
        if ctx.command and ctx.command.has_error_handler():
            return
        if ctx.cog and ctx.cog.has_error_handler():
            return

        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("this only works in a server twin☹️☹️")
        elif isinstance(error, commands.NotOwner):
            await ctx.send("only the bot owner can do that twin☹️☹️")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"you forgot `{error.param.name}` twin☹️☹️")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("that doesn't look right twin☹️☹️")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send("you can't do that twin☹️☹️")
        else:
            traceback.print_exception(type(error), error, error.__traceback__)
            await ctx.send("something broke twin☹️☹️ not your fault")


bot = ImageBot()


@bot.check
async def guild_only(ctx: commands.Context) -> bool:
    # everything is stored per guild, so DMs make no sense
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return True


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN missing, add it to your .env")
    bot.run(TOKEN)
