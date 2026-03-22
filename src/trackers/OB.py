# Upload Assistant © 2025 Audionut & wastaken7 — Licensed under UAPL v1.0
import os
import re
from typing import Any, Optional, Union, cast
from urllib.parse import urlparse

import aiofiles
import httpx
from bs4 import BeautifulSoup
from unidecode import unidecode

from src.console import console
from src.cookie_auth import CookieValidator
from src.exceptions import *  # noqa E403
from src.trackers.COMMON import COMMON

from src.trackers.TJUPT import TJUPT

Meta = dict[str, Any]
Config = dict[str, Any]


class OB:
    """OurBits (ourbits.club) tracker — NexusPHP based."""

    # Reuse TJUPT's area mapping (same NexusPHP region IDs)
    AREA_MAP = TJUPT.AREA_MAP

    def __init__(self, config: Config) -> None:
        self.config: Config = config
        self.tracker = 'OB'
        self.source_flag = 'OB'
        self.passkey = str(config['TRACKERS']['OB'].get('passkey', '')).strip()
        self.username = str(config['TRACKERS']['OB'].get('username', '')).strip()
        self.password = str(config['TRACKERS']['OB'].get('password', '')).strip()
        self.rehost_images = bool(config['TRACKERS']['OB'].get('img_rehost', False))
        self.ptgen_api = str(config['TRACKERS']['OB'].get('ptgen_api', '')).strip()

        self.ptgen_retry = 3
        self.signature: Optional[str] = None
        self.banned_groups: list[str] = [""]
        self.timeout_search = int(config['TRACKERS']['OB'].get('timeout_search', 15))
        self.timeout_upload = int(config['TRACKERS']['OB'].get('timeout_upload', 60))

        self.cookie_validator = CookieValidator(config)

    async def validate_credentials(self, meta: Meta) -> bool:
        vcookie = await self.validate_cookies(meta)
        if vcookie is not True:
            console.print('[red]Failed to validate cookies. Please confirm that the site is up and your passkey is valid.')
            return False
        return True

    async def validate_cookies(self, meta: Meta) -> bool:
        common = COMMON(config=self.config)
        url = "https://ourbits.club"
        cookiefile = f"{meta['base_dir']}/data/cookies/OB.txt"
        if os.path.exists(cookiefile):
            cookies = await common.parseCookieFile(cookiefile)
            async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url=url)
                return 'logout.php' in resp.text
        else:
            console.print("[bold red]Missing Cookie File. (data/cookies/OB.txt)")
            return False

    async def search_existing(self, meta: Meta, _disctype: str) -> Union[list[str], bool]:
        dupes: list[str] = []
        common = COMMON(config=self.config)
        cookiefile = f"{meta['base_dir']}/data/cookies/OB.txt"
        if not os.path.exists(cookiefile):
            console.print("[bold red]Missing Cookie File. (data/cookies/OB.txt)")
            return False
        cookies = await common.parseCookieFile(cookiefile)
        imdb_id = int(meta.get('imdb_id', 0) or 0)
        imdb = f"tt{meta.get('imdb', '')}" if imdb_id != 0 else ""
        search_url = f"https://ourbits.club/torrents.php?search={imdb}&incldead=0&search_mode=0"

        try:
            async with httpx.AsyncClient(cookies=cookies, timeout=float(self.timeout_search), follow_redirects=True) as client:
                response = await client.get(search_url)

                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'lxml')
                    rows = soup.select('table.torrents > tr:has(table.torrentname)')
                    for row in rows:
                        text = row.select_one('a[href^="details.php?id="]')
                        if text is not None:
                            release_value = text.attrs.get('title', '')
                            release = str(release_value)
                            if release:
                                dupes.append(release)
                else:
                    console.print(f"[bold red]HTTP request failed. Status: {response.status_code}")

        except httpx.TimeoutException:
            console.print("[bold red]Request timed out while searching for existing torrents.")
        except httpx.RequestError as e:
            console.print(f"[bold red]An error occurred while making the request: {e}")
        except Exception as e:
            console.print(f"[bold red]Unexpected error: {e}")
            console.print_exception()

        return dupes

    async def get_type_category_id(self, meta: Meta) -> str:
        cat_id = "0"  # Default to "请选择"
        category = str(meta.get('category', ''))

        genres_value = meta.get("genres", "")
        genres = ', '.join(cast(list[str], genres_value)) if isinstance(genres_value, list) else str(genres_value)
        keywords_value = meta.get("keywords", "")
        keywords = ', '.join(cast(list[str], keywords_value)) if isinstance(keywords_value, list) else str(keywords_value)
        genres_lower = genres.lower()
        keywords_lower = keywords.lower()

        # Genre-based categories take priority over basic MOVIE/TV
        if 'animation' in genres_lower or 'animation' in keywords_lower or 'anime' in genres_lower:
            cat_id = '405'  # 动漫
        elif 'documentary' in genres_lower or 'documentary' in keywords_lower:
            cat_id = '402'  # 纪录片
        elif 'variety' in genres_lower or 'reality' in genres_lower or 'talk show' in genres_lower:
            cat_id = '403'  # 综艺
        elif category == 'MOVIE':
            cat_id = '401'  # 电影
        elif category == 'TV':
            cat_id = '404'  # 剧集/TV Series

        return cat_id

    async def get_type_medium_id(self, meta: Meta) -> str:
        medium_id = "0"
        # 1 = UHD Discs, 2 = BD Discs
        if meta.get('is_disc', '') in ("BDMV", "HD DVD"):
            medium_id = '1' if meta['resolution'] == '2160p' else '2'
        elif meta.get('is_disc', '') == "DVD":
            medium_id = '7'
        elif meta.get('type', '') == "HDTV":
            medium_id = '4'
        elif meta.get('type', '') in ("ENCODE", "WEBRIP"):
            medium_id = '6'
        elif meta.get('type', '') == "REMUX":
            medium_id = '3'
        elif meta.get('type', '') == "WEBDL":
            medium_id = '5'

        if medium_id == "0":
            console.print("[yellow]OB: Could not determine medium type, defaulting to 0[/yellow]")

        return medium_id

    async def get_area_id(self, meta: Meta) -> int:
        ptgen = cast(dict[str, Any], meta.get('ptgen', {}))
        regions_value = ptgen.get("region", [])
        regions = cast(list[str], regions_value) if isinstance(regions_value, list) else []
        for area, area_id in self.AREA_MAP.items():
            if area in regions:
                return area_id
        return 8

    async def edit_name(self, meta: Meta) -> str:
        ob_name = str(meta.get('name', ''))

        remove_list = ['Dubbed', 'Dual-Audio']
        for each in remove_list:
            ob_name = ob_name.replace(each, '')

        ob_name = ob_name.replace('PQ10', 'HDR')

        if meta.get('type') == 'WEBDL' and meta.get('has_encode_settings', False) is True:
            ob_name = ob_name.replace('H.264', 'x264')

        return ob_name

    async def edit_desc(self, meta: Meta) -> None:
        async with aiofiles.open(f"{meta['base_dir']}/tmp/{meta['uuid']}/DESCRIPTION.txt", encoding='utf-8') as base_file:
            base = await base_file.read()

        from src.bbcode import BBCODE
        from src.trackers.COMMON import COMMON
        common = COMMON(config=self.config)

        parts: list[str] = []

        if int(meta.get('imdb_id', 0) or 0) != 0:
            ptgen = await common.ptgen(meta, self.ptgen_api, self.ptgen_retry)
            if ptgen.strip() != '':
                parts.append(ptgen)

        bbcode = BBCODE()
        if meta.get('discs', []) != []:
            discs = cast(list[dict[str, Any]], meta.get('discs', []))
            for each in discs:
                if each['type'] == "BDMV":
                    parts.append(f"[hide=BDInfo]{each['summary']}[/hide]\n")
                    parts.append("\n")
                if each['type'] == "DVD":
                    parts.append(f"{each['name']}:\n")
                    parts.append(f"[hide=mediainfo][{each['vob_mi']}[/hide] [hide=mediainfo][{each['ifo_mi']}[/hide]\n")
                    parts.append("\n")
        else:
            async with aiofiles.open(f"{meta['base_dir']}/tmp/{meta['uuid']}/MEDIAINFO_CLEANPATH.txt", encoding='utf-8') as mi_file:
                mi = await mi_file.read()
            parts.append(f"[hide=mediainfo]{mi}[/hide]")
            parts.append("\n")
        desc = base
        desc = bbcode.convert_code_to_quote(desc)
        desc = bbcode.convert_spoiler_to_hide(desc)
        desc = bbcode.convert_comparison_to_centered(desc, 1000)
        desc = desc.replace('[img]', '[img]')
        desc = re.sub(r"(\[img=\d+)]", "[img]", desc, flags=re.IGNORECASE)
        parts.append(desc)

        images = cast(list[dict[str, Any]], meta.get('image_list', []))
        if len(images) > 0:
            parts.append("[center]")
            for each in range(len(images[:int(meta['screens'])])):
                web_url = images[each]['web_url']
                img_url = images[each]['img_url']
                parts.append(f"[url={web_url}][img]{img_url}[/img][/url]")
            parts.append("[/center]")

        if self.signature is not None:
            parts.append("\n\n")
            parts.append(self.signature)

        async with aiofiles.open(f"{meta['base_dir']}/tmp/{meta['uuid']}/[{self.tracker}]DESCRIPTION.txt", 'w', encoding='utf-8') as descfile:
            await descfile.write("".join(parts))

    async def is_zhongzi(self, meta: Meta) -> Optional[str]:
        if meta.get('is_disc', '') != 'BDMV':
            mi = cast(dict[str, Any], meta.get('mediainfo', {}))
            media = cast(dict[str, Any], mi.get('media', {}))
            tracks = cast(list[dict[str, Any]], media.get('track', []))
            for track in tracks:
                if track['@type'] == "Text":
                    language = track.get('Language')
                    if language == "zh":
                        return 'yes'
        else:
            bdinfo = cast(dict[str, Any], meta.get('bdinfo', {}))
            subtitles = cast(list[str], bdinfo.get('subtitles', []))
            for language in subtitles:
                if language == "Chinese":
                    return 'yes'
        return None

    async def upload(self, meta: Meta, _disctype: str) -> bool:

        common = COMMON(config=self.config)
        await common.create_torrent_for_upload(meta, self.tracker, self.source_flag)

        desc_file = f"{meta['base_dir']}/tmp/{meta['uuid']}/[{self.tracker}]DESCRIPTION.txt"
        if not os.path.exists(desc_file):
            await self.edit_desc(meta)

        # Check anonymous upload
        anon = None
        if meta.get('anon') == 1 or self.config['TRACKERS'][self.tracker].get('anon', False):
            anon = 'yes'

        ob_name = await self.edit_name(meta)

        async with aiofiles.open(desc_file, encoding='utf-8') as desc_handle:
            ob_desc = await desc_handle.read()
        torrent_path = f"{meta['base_dir']}/tmp/{meta['uuid']}/[{self.tracker}].torrent"

        async with aiofiles.open(torrent_path, 'rb') as torrentFile:
            torrent_bytes = await torrentFile.read()
        filelist = cast(list[Any], meta.get('filelist', []))
        if len(filelist) == 1:
            torrentFileName = unidecode(os.path.basename(str(meta.get('video', ''))).replace(' ', '.'))
        else:
            torrentFileName = unidecode(os.path.basename(str(meta.get('path', ''))).replace(' ', '.'))
        files = {
            'file': (f"{torrentFileName}.torrent", torrent_bytes, "application/x-bittorent"),
        }

        # use chinese small_descr
        ptgen = cast(dict[str, Any], meta.get('ptgen', {}))
        trans_title = cast(list[str], ptgen.get("trans_title", []))
        genres = cast(list[str], ptgen.get("genre", []))
        if trans_title != ['']:
            small_descr = ''
            for title_ in trans_title:
                small_descr += f'{title_} / '
            genre_value = genres[0] if genres else ''
            small_descr += "| 类别:" + genre_value
            small_descr = small_descr.replace('/ |', '|')
        else:
            small_descr = str(meta.get('title', ''))

        # Build form data
        data: dict[str, Any] = {
            "name": ob_name,
            "small_descr": small_descr,
            "descr": ob_desc,
            "type": await self.get_type_category_id(meta),
        }

        # Add IMDb URL if available
        imdb_id = int(meta.get('imdb_id', 0) or 0)
        if imdb_id != 0:
            data["url"] = f"https://www.imdb.com/title/tt{meta.get('imdb', '')}/"

        # Add anonymous upload checkbox if needed
        if anon:
            data["uplver"] = anon

        # Add Chinese subtitle checkbox if detected
        chinese_sub = await self.is_zhongzi(meta)
        if chinese_sub == 'yes':
            data["chinese"] = "yes"

        url = "https://ourbits.club/takeupload.php"

        # Submit
        if meta.get('debug'):
            console.print(url)
            console.print(data)
            meta['tracker_status'][self.tracker]['status_message'] = "Debug mode enabled, not uploading."
            await common.create_torrent_for_upload(meta, f"{self.tracker}" + "_DEBUG", f"{self.tracker}" + "_DEBUG", announce_url="https://fake.tracker")
            return True  # Debug mode - simulated success
        else:
            cookiefile = f"{meta['base_dir']}/data/cookies/OB.txt"
            if os.path.exists(cookiefile):
                cookies = await common.parseCookieFile(cookiefile)
                async with httpx.AsyncClient(cookies=cookies, timeout=float(self.timeout_upload), follow_redirects=True) as client:
                    up = await client.post(url=url, data=data, files=files)

                    if str(up.url).startswith("https://ourbits.club/details.php?id="):
                        console.print(f"[green]Uploaded to: [yellow]{str(up.url).replace('&uploaded=1', '')}[/yellow][/green]")
                        id_match = re.search(r"(id=)(\d+)", urlparse(str(up.url)).query)
                        if id_match is None:
                            raise UploadException("Upload succeeded but torrent id was not present in the redirect URL.", 'red')  # noqa: F405
                        torrent_id = id_match.group(2)
                        await self.download_new_torrent(torrent_id, torrent_path)
                        meta['tracker_status'][self.tracker]['status_message'] = str(up.url).replace('&uploaded=1', '')
                        meta['tracker_status'][self.tracker]['torrent_id'] = torrent_id
                        return True
                    else:
                        console.print(data)
                        console.print("\n\n")
                        raise UploadException(f"Upload to OB Failed: result URL {up.url} ({up.status_code}) was not expected", 'red')  # noqa #F405
        return False

    async def download_new_torrent(self, id: str, torrent_path: str) -> None:
        download_url = f"https://ourbits.club/download.php?id={id}&passkey={self.passkey}"
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            r = await client.get(url=download_url)
        if r.status_code == 200:
            async with aiofiles.open(torrent_path, "wb") as tor:
                await tor.write(r.content)
        else:
            console.print("[red]There was an issue downloading the new .torrent from OurBits")
            console.print(r.text)
