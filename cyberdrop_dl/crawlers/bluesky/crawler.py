from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from cyberdrop_dl.crawlers.bluesky.api import BlueskyAPI
from cyberdrop_dl.crawlers.crawler import Crawler, SupportedDomains, SupportedPaths
from cyberdrop_dl.mediaprops import Resolution
from cyberdrop_dl.url_objects import AbsoluteHttpURL
from cyberdrop_dl.utils.errors import error_handling_wrapper

if TYPE_CHECKING:
    from cyberdrop_dl.url_objects import ScrapeItem

_BLOB_ENDPOINT = AbsoluteHttpURL("https://bsky.social/xrpc/com.atproto.sync.getBlob")


class BlueskyCrawler(Crawler):
    SUPPORTED_DOMAINS: ClassVar[SupportedDomains] = ("bsky.app", "bsky.social", "main.bsky.dev")
    SUPPORTED_PATHS: ClassVar[SupportedPaths] = {
        "Post": "/profile/<handle>/post/<post_id>",
        "Profile": "/profile/<handle>",
    }
    PRIMARY_URL: ClassVar[AbsoluteHttpURL] = AbsoluteHttpURL("https://bsky.app")
    DOMAIN: ClassVar[str] = "bluesky"
    DEFAULT_POST_TITLE_FORMAT: ClassVar[str] = "{date:%Y-%m-%d} - {id}"

    def __post_init__(self) -> None:
        self.api: BlueskyAPI = BlueskyAPI.from_crawler(self)

    @property
    def separate_posts(self) -> bool:
        return True

    async def fetch(self, scrape_item: ScrapeItem) -> None:
        parts = scrape_item.url.parts[1:]
        if len(parts) >= 4 and parts[0] == "profile" and parts[2] == "post":
            await self.post(scrape_item, parts[1], parts[3])
        elif len(parts) == 2 and parts[0] == "profile":
            await self.user(scrape_item, parts[1], "posts_and_author_threads")
        else:
            raise ValueError

    @error_handling_wrapper
    async def post(self, scrape_item: ScrapeItem, actor: str, post_id: str) -> None:
        for post in await self.api.post_thread(actor, post_id):
            new_item = (
                scrape_item
                if post["uri"].endswith(f"/{post_id}")
                else scrape_item.create_child(self.parse_url(self._post_url(post)))
            )
            self._post(new_item, post)
            if new_item is not scrape_item:
                scrape_item.add_children()

    @error_handling_wrapper
    async def user(self, scrape_item: ScrapeItem, actor: str, feed_filter: str) -> None:
        scrape_item.setup_as_profile("")
        async for page in self.api.author_feed(actor, feed_filter):
            for entry in page:
                post = entry.get("post", entry)
                new_item = scrape_item.create_child(self.parse_url(self._post_url(post)))
                self._post(new_item, post)
                scrape_item.add_children()

    def _post(self, scrape_item: ScrapeItem, post: dict[str, Any]) -> None:
        record = post["record"]
        author = post["author"]
        post_id = post["uri"].rpartition("/")[2]
        scrape_item.setup_as_post(self.create_title(f"@{author['handle']}"))
        scrape_item.uploaded_at = date = self.parse_iso_date(record["createdAt"])
        scrape_item.append_folders(self.create_separate_post_title(None, post_id, date))
        self.create_eager_task(self.write_metadata(scrape_item, f"post {post_id}", post))

        embed = post.get("embed", {})
        self._extract_videos(scrape_item, embed, post_id)
        self._extract_images(scrape_item, embed, record.get("embed", {}).get("images", ()), author["did"])

    def _extract_videos(self, scrape_item: ScrapeItem, embed: dict[str, Any], post_id: str) -> None:
        for media in self._media(embed):
            if playlist := media.get("playlist"):
                self.create_eager_task(self._video(scrape_item, playlist, post_id, media))
                scrape_item.add_children()

    def _extract_images(self, scrape_item: ScrapeItem, embed: dict[str, Any], record_images: Any, did: str) -> None:
        image_index = 0
        for media in self._media(embed):
            if "playlist" in media:
                continue

            if fullsize := media.get("fullsize"):
                image = record_images[image_index] if image_index < len(record_images) else {}
                image_index += 1
                blob = image.get("image", {})
                cid = blob.get("ref", {}).get("$link") or self.parse_url(fullsize, trim=False).name
                _, ext = self.get_filename_and_ext(cid, mime_type=blob.get("mimeType"))
                image_url = self._blob_url(did, cid)
                self.create_eager_task(
                    self.handle_file(image_url, scrape_item, cid + ext, ext, custom_filename=cid + ext)
                )
                scrape_item.add_children()
                continue

            if "mimeType" not in media:
                continue

            cid = media["ref"]["$link"] if "ref" in media else media["cid"]
            ext = "." + media["mimeType"].partition("/")[2]
            url = self._blob_url(did, cid)
            self.create_eager_task(self.handle_file(url, scrape_item, cid + ext, ext, custom_filename=cid + ext))
            scrape_item.add_children()

    async def _video(self, scrape_item: ScrapeItem, playlist: str, post_id: str, media: dict[str, Any]) -> None:
        playlist_url = self.parse_url(playlist, trim=False)
        with self.catch_errors(playlist_url):
            manifest, info = await self.request_m3u8(playlist_url)
            aspect_ratio = media.get("aspectRatio", {})
            resolution = (
                info.resolution
                if info
                else Resolution.parse(aspect_ratio.get("height") if aspect_ratio.get("width") else None)
            )
            filename = self.create_custom_filename(post_id, ".mp4", resolution=resolution)
            await self.handle_file(
                playlist_url,
                scrape_item,
                post_id,
                ".mp4",
                m3u8=manifest,
                custom_filename=filename,
            )

    @staticmethod
    def _media(embed: dict[str, Any]) -> list[dict[str, Any]]:
        media = embed.get("media", embed)
        if "playlist" in media:
            return [media]

        files = [image.get("image", image) for image in media.get("images", ())]
        if video := media.get("video"):
            files.append(video)
        return files

    def _post_url(self, post: dict[str, Any]) -> str:
        author = post["author"]["handle"]
        post_id = post["uri"].rpartition("/")[2]
        return f"{self.PRIMARY_URL}/profile/{author}/post/{post_id}"

    @staticmethod
    def _blob_url(did: str, cid: str) -> AbsoluteHttpURL:
        return _BLOB_ENDPOINT.with_query(did=did, cid=cid)
