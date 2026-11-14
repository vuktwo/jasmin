"""Image-only Discord commands: save, retrieve, and delete guild images."""

from pathlib import Path

import discord
from discord.ext import commands

from .imagestorage import (
    DuplicateImage,
    StorageFull,
    clean_name,
)


class ImageListView(discord.ui.View):
    """Small button paginator for a !list result."""

    PAGE_SIZE = 10

    def __init__(self, author_id: int, images: list[dict], tag: str | None):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.images = images
        self.tag = tag
        self.page = 0
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _embed(self) -> discord.Embed:
        start = self.page * self.PAGE_SIZE
        page_images = self.images[start:start + self.PAGE_SIZE]
        total_pages = max(1, (len(self.images) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        title = "saved images" if self.tag is None else f"saved images tagged `{self.tag}`"
        lines = []
        for meta in page_images:
            tags = f" — {', '.join(meta['tags'])}" if meta["tags"] else ""
            lines.append(f"`{meta['name']}`{tags}")
        embed = discord.Embed(title=title, description="\n".join(lines), color=5242876)
        embed.set_footer(text=f"{len(self.images)} image(s) • page {self.page + 1}/{total_pages}")
        return embed

    def _sync_buttons(self) -> None:
        total_pages = max(1, (len(self.images) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("this list belongs to someone else twin", ephemeral=True)
        return False

    async def _update(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._update(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._update(interaction)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            await self.message.edit(view=self)


class SidImage(commands.Cog):
    """Commands that operate exclusively on images already managed by Storage."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    async def _save_attachment(ctx: commands.Context) -> discord.Attachment | None:
        """Return a directly attached image or one image from a replied message.

        A direct attachment takes precedence, so `!save cat` can be sent with an
        image attached. Both sources require exactly one image so we never
        silently choose the wrong file.
        """
        if ctx.message.attachments:
            images = [
                attachment
                for attachment in ctx.message.attachments
                if attachment.content_type
                and attachment.content_type.startswith("image/")
            ]
            if len(images) != 1:
                await ctx.send("attach exactly one image to `!save`, or reply to one twin")
                return None
            return images[0]

        if ctx.message.reference is None or ctx.message.reference.message_id is None:
            await ctx.send("attach one image to `!save`, or reply to one first twin")
            return None

        try:
            target = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except discord.NotFound:
            await ctx.send("i couldn't find the message you replied to twin")
            return None
        except discord.Forbidden:
            await ctx.send("i can't read that replied message twin")
            return None

        images = [
            attachment
            for attachment in target.attachments
            if attachment.content_type and attachment.content_type.startswith("image/")
        ]
        if len(images) != 1:
            await ctx.send("the replied message needs exactly one image twin")
            return None
        return images[0]

    @commands.command(name="save", aliases=["saveimage", "saveimg", "s"])
    async def save(self, ctx: commands.Context, name: str | None = None):
        """Save one directly attached image or one image in a replied message."""
        store = self.bot.storage.guild(ctx.guild.id)
        if not store.is_registered():
            await ctx.send("this server isn't registered yet twin")
            return

        clean = clean_name(name)
        if clean is None:
            await ctx.send("pick a name with 1-32 letters, numbers, `_`, or `-` twin")
            return

        attachment = await self._save_attachment(ctx)
        if attachment is None:
            return

        try:
            data = await attachment.read(use_cached=False)
        except discord.HTTPException:
            await ctx.send("i couldn't download that image twin, try again")
            return

        ext = Path(attachment.filename).suffix.lstrip(".") or "png"
        try:
            meta = await store.save_image(clean, data, ext, ctx.author.id)
        except StorageFull:
            await ctx.send("this server has used its whole 1 GB image stash twin")
            return
        except DuplicateImage as error:
            await ctx.send(f"that's already saved as `{error.existing_name}` twin")
            return

        if meta["name"] == clean:
            await ctx.send(f"done twin, saved it as `{meta['name']}`")
        else:
            await ctx.send(
                f"`{clean}` was taken, so i saved it as `{meta['name']}` twin"
            )

    @commands.command(name="image", aliases=["img", "i", "fetch"])
    async def image(self, ctx: commands.Context, name: str | None = None):
        """Send an untouched saved image into the current channel."""
        store = self.bot.storage.guild(ctx.guild.id)
        if not store.is_registered():
            await ctx.send("this server isn't registered yet twin")
            return

        clean = clean_name(name)
        if clean is None:
            await ctx.send("give me the image name twin")
            return

        meta = store.get_meta(clean)
        if meta is None:
            await ctx.send(f"i don't have an image called `{clean}` twin")
            return

        path = store.image_path(meta)
        if not path.exists():
            await ctx.send("the image file is missing, but its record is still here twin")
            return
        await ctx.send(file=discord.File(path, filename=path.name))

    @commands.command(name="list", aliases=["images"])
    async def list_images(self, ctx: commands.Context, tag: str | None = None):
        """List saved image names, optionally filtered by one tag."""
        store = self.bot.storage.guild(ctx.guild.id)
        if not store.is_registered():
            await ctx.send("this server isn't registered yet twin")
            return

        tag = tag.strip().lower() if tag else None
        images = store.list_images(tag)
        if not images:
            if tag:
                await ctx.send(f"nothing is tagged `{tag}` yet twin")
            else:
                await ctx.send("there are no saved images yet twin")
            return

        view = ImageListView(ctx.author.id, images, tag)
        view.message = await ctx.send(embed=view._embed(), view=view)

    @commands.command(name="delete", aliases=["deleteimage", "del", "remove"])
    async def delete(self, ctx: commands.Context, name: str | None = None):
        """Delete a saved image. Guild whitelist members may do this."""
        store = self.bot.storage.guild(ctx.guild.id)
        if not store.is_registered():
            await ctx.send("this server isn't registered yet twin")
            return
        if not store.is_whitelisted(ctx.author.id):
            await ctx.send("only whitelisted people can delete images twin")
            return

        clean = clean_name(name)
        if clean is None:
            await ctx.send("give me the image name to delete twin")
            return

        deleted = await store.delete_image(clean)
        if deleted is None:
            await ctx.send(f"i don't have an image called `{clean}` twin")
            return
        await ctx.send(f"deleted `{deleted['name']}` twin")


async def setup(bot: commands.Bot):
    await bot.add_cog(SidImage(bot))
