from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from cyberdrop_dl.crawlers.bluesky.api import BlueskyAPI
from cyberdrop_dl.crawlers.crawler import Crawler, SupportedDomains, SupportedPaths
from cyberdrop_dl.mediaprops import Resolution
from cyberdrop_dl.url_objects import AbsoluteHttpURL
from cyberdrop_dl.utils.errors import error_handling_wrapper

if TYPE_CHECKING:
    from cyberdrop_dl.url_objects import ScrapeItem

_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}


class BlueskyCrawler(Crawler):
    SUPPORTED_DOMAINS: ClassVar[SupportedDomains] = ("bsky.app", "bsky.social", "main.bsky.dev")
    SUPPORTED_PATHS: ClassVar[SupportedPaths] = {
        "Post": "/profile/<handle>/post/<post_id>",
        "User posts": "/profile/<handle>",
        "User media": "/profile/<handle>/media",
        "User replies": "/profile/<handle>/replies",
        "User videos": "/profile/<handle>/video",
        "Feed": "/profile/<handle>/feed/<feed_id>",
        "List": "/profile/<handle>/lists/<list_id>",
        "Hashtag": "/hashtag/<tag>",
        "Search": "/search?q=<query>",
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
        query = scrape_item.url.query.get("q")
        if len(parts) >= 4 and parts[0] == "profile" and parts[2] == "post":
            await self.post(scrape_item, parts[1], parts[3])
        elif len(parts) == 2 and parts[0] == "profile":
            await self.user(scrape_item, parts[1], "posts_and_author_threads")
        elif len(parts) == 3 and parts[0] == "profile" and parts[2] in {"media", "replies", "video", "likes"}:
            feed_filter = {
                "media": "posts_with_media",
                "replies": "posts_with_replies",
                "video": "posts_with_video",
                "likes": "posts_with_media",
            }[parts[2]]
            await self.user(scrape_item, parts[1], feed_filter, likes=parts[2] == "likes")
        elif len(parts) == 4 and parts[0] == "profile" and parts[2] in {"feed", "lists"}:
            await self.custom_feed(scrape_item, parts[1], parts[3], is_feed=parts[2] == "feed")
        elif len(parts) == 2 and parts[0] == "hashtag":
            await self.search(scrape_item, f"#{parts[1]}")
        elif parts == ("search",) and query:
            await self.search(scrape_item, query)
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
    async def user(self, scrape_item: ScrapeItem, actor: str, feed_filter: str, *, likes: bool = False) -> None:
        scrape_item.setup_as_profile("")
        pages = self.api.likes(actor) if likes else self.api.author_feed(actor, feed_filter)
        async for page in pages:
            for entry in page:
                post = entry.get("post", entry)
                new_item = scrape_item.create_child(self.parse_url(self._post_url(post)))
                self._post(new_item, post)
                scrape_item.add_children()

    @error_handling_wrapper
    async def custom_feed(self, scrape_item: ScrapeItem, actor: str, feed_id: str, *, is_feed: bool) -> None:
        scrape_item.setup_as_forum("")
        pages = self.api.feed(actor, feed_id) if is_feed else self.api.list_feed(actor, feed_id)
        async for page in pages:
            for entry in page:
                post = entry.get("post", entry)
                new_item = scrape_item.create_child(self.parse_url(self._post_url(post)))
                self._post(new_item, post)
                scrape_item.add_children()

    @error_handling_wrapper
    async def search(self, scrape_item: ScrapeItem, query: str) -> None:
        scrape_item.setup_as_forum("")
        async for page in self.api.search(query):
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

        record_images = record.get("embed", {}).get("images", ())
        image_index = 0
        for media in self._media(post.get("embed", {})):
            if playlist := media.get("playlist"):
                self.create_eager_task(self._video(scrape_item, playlist, post_id, media))
                scrape_item.add_children()
                continue

            if fullsize := media.get("fullsize"):
                image = record_images[image_index] if image_index < len(record_images) else {}
                image_index += 1
                blob = image.get("image", {})
                cid = blob.get("ref", {}).get("$link") or self.parse_url(fullsize, trim=False).name
                mime_type = blob.get("mimeType")
                ext = _IMAGE_EXTENSIONS.get(mime_type, ".jpg")
                image_url = self._blob_url(author["did"], cid)
                self.create_eager_task(
                    self.handle_file(image_url, scrape_item, cid + ext, ext, custom_filename=cid + ext)
                )
                scrape_item.add_children()
                continue

            did = author["did"]
            cid = media["ref"]["$link"] if "ref" in media else media["cid"]
            ext = "." + media["mimeType"].partition("/")[2]
            url = self._blob_url(did, cid)
            self.create_eager_task(self.handle_file(url, scrape_item, cid + ext, ext, custom_filename=cid + ext))
            scrape_item.add_children()

    async def _video(self, scrape_item: ScrapeItem, playlist: str, post_id: str, media: dict[str, Any]) -> None:
        playlist_url = self.parse_url(playlist, trim=False)
        with self.catch_errors(playlist_url):
            manifest, _info = await self.request_m3u8(playlist_url)
            resolution = Resolution(media["aspectRatio"]["width"], media["aspectRatio"]["height"])
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
        return AbsoluteHttpURL("https://bsky.social/xrpc/com.atproto.sync.getBlob").with_query(did=did, cid=cid)
