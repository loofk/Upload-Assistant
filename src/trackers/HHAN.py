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

Meta = dict[str, Any]
Config = dict[str, Any]


class HHAN:

    def __init__(self, config: Config) -> None:
        self.config: Config = config
        self.tracker = 'HHAN'
        self.source_flag = 'HHAN'
        self.passkey = str(config['TRACKERS']['HHAN'].get('passkey', '')).strip()
        self.username = str(config['TRACKERS']['HHAN'].get('username', '')).strip()
        self.password = str(config['TRACKERS']['HHAN'].get('password', '')).strip()
        self.rehost_images = bool(config['TRACKERS']['HHAN'].get('img_rehost', False))
        self.ptgen_api = str(config['TRACKERS']['HHAN'].get('ptgen_api', '')).strip()

        self.ptgen_retry = 3
        self.signature: Optional[str] = None
        self.banned_groups: list[str] = [""]

        self.cookie_validator = CookieValidator(config)

    async def validate_credentials(self, meta: Meta) -> bool:
        vcookie = await self.validate_cookies(meta)
        if vcookie is not True:
            console.print('[red]Failed to validate cookies. Please confirm that the site is up and your passkey is valid.')
            return False
        return True

    async def validate_cookies(self, meta: Meta) -> bool:
        common = COMMON(config=self.config)
        url = "https://hhanclub.net"
        cookiefile = f"{meta['base_dir']}/data/cookies/HHAN.txt"
        if os.path.exists(cookiefile):
            cookies = await common.parseCookieFile(cookiefile)
            async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url=url)

                return resp.text.find('''<a href="#" data-url="logout.php" id="logout-confirm">''') != -1
        else:
            console.print("[bold red]Missing Cookie File. (data/cookies/HHAN.txt)")
            return False

    async def search_existing(self, meta: Meta, _disctype: str) -> Union[list[str], bool]:
        dupes: list[str] = []
        common = COMMON(config=self.config)
        cookiefile = f"{meta['base_dir']}/data/cookies/HHAN.txt"
        if not os.path.exists(cookiefile):
            console.print("[bold red]Missing Cookie File. (data/cookies/HHAN.txt)")
            return False
        cookies = await common.parseCookieFile(cookiefile)
        imdb_id = int(meta.get('imdb_id', 0) or 0)
        imdb = f"tt{meta.get('imdb', '')}" if imdb_id != 0 else ""
        source = await self.get_type_medium_id(meta)
        search_url = f"https://hhanclub.net/torrents.php?search={imdb}&incldead=0&search_mode=0&source{source}=1"

        try:
            async with httpx.AsyncClient(cookies=cookies, timeout=10.0, follow_redirects=True) as client:
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

    async def get_info_from_torrent_id(self, hhan_id: Union[int, str], meta: Optional[Meta] = None) -> tuple[Optional[int], Optional[int], Optional[str], Optional[str], Optional[str]]:
        """
        Fetch metadata from HHAN torrent details page using torrent ID.
        Returns: (imdb_id, tmdb_id, name, torrenthash, description)
        """
        hhan_imdb = hhan_tmdb = hhan_name = hhan_torrenthash = hhan_description = None
        common = COMMON(config=self.config)
        base_dir = meta.get('base_dir', '') if meta else ''
        cookiefile = f"{base_dir}/data/cookies/HHAN.txt"
        
        if not os.path.exists(cookiefile):
            console.print("[bold red]Missing Cookie File. (data/cookies/HHAN.txt)[/bold red]")
            return hhan_imdb, hhan_tmdb, hhan_name, hhan_torrenthash, hhan_description
        
        cookies = await common.parseCookieFile(cookiefile)
        url = f"https://hhanclub.net/details.php?id={hhan_id}"
        
        try:
            async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
                response = await client.get(url)
                
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'lxml')
                    
                    # Extract IMDb ID
                    imdb_link = soup.select_one('a[href*="imdb.com/title/tt"]')
                    if not imdb_link:
                        for link in soup.find_all('a', href=True):
                            href = link.get('href', '')
                            if 'imdb.com/title/tt' in href:
                                imdb_link = link
                                break
                    if imdb_link:
                        imdb_href = imdb_link.get('href', '')
                        imdb_match = re.search(r'tt(\d+)', imdb_href)
                        if imdb_match:
                            hhan_imdb = int(imdb_match.group(1))
                    
                    # Extract TMDb ID
                    tmdb_link = soup.select_one('a[href*="themoviedb.org"]')
                    if not tmdb_link:
                        for link in soup.find_all('a', href=True):
                            href = link.get('href', '')
                            if 'themoviedb.org' in href:
                                tmdb_link = link
                                break
                    if tmdb_link:
                        tmdb_href = tmdb_link.get('href', '')
                        tmdb_match = re.search(r'/(movie|tv)/(\d+)', tmdb_href)
                        if tmdb_match:
                            hhan_tmdb = int(tmdb_match.group(2))
                    
                    # Extract Douban ID
                    douban_link = soup.select_one('a[href*="movie.douban.com/subject/"]')
                    if not douban_link:
                        for link in soup.find_all('a', href=True):
                            href = link.get('href', '')
                            if 'movie.douban.com/subject/' in href or 'douban.com/subject/' in href:
                                douban_link = link
                                break
                    if douban_link:
                        douban_href = douban_link.get('href', '')
                        douban_match = re.search(r'/subject/(\d+)', douban_href)
                        if douban_match and meta:
                            douban_id = douban_match.group(1)
                            meta['douban_id'] = meta['douban'] = douban_id
                            console.print(f"[green]HHAN: Found Douban ID: {douban_id}[/green]")
                    if not douban_link and meta:
                        douban_url_match = re.search(r'https?://movie\.douban\.com/subject/(\d+)', response.text)
                        if douban_url_match:
                            douban_id = douban_url_match.group(1)
                            meta['douban_id'] = meta['douban'] = douban_id
                            console.print(f"[green]HHAN: Found Douban ID in page text: {douban_id}[/green]")
                    
                    # Extract torrent name
                    name_elem = soup.select_one('h1, .torrentname, td.torrentname, b.torrentname, table.torrentname')
                    if name_elem:
                        hhan_name = name_elem.get_text(strip=True)
                    
                    # Extract description
                    desc_elem = soup.select_one('#desctext, .desctext, td[colspan="2"], .nfo')
                    if desc_elem:
                        hhan_description = str(desc_elem)
                    
                    # Extract torrent hash
                    hash_elem = soup.select_one('input[name="hash"], code, .hash')
                    if hash_elem:
                        hash_text = hash_elem.get_text(strip=True)
                        if len(hash_text) == 40:
                            hhan_torrenthash = hash_text
                    
                else:
                    console.print(f"[yellow]Failed to fetch HHAN details page. Status: {response.status_code}[/yellow]")
                    
        except httpx.RequestError as e:
            console.print(f"[red]Request error fetching HHAN details: {e}[/red]")
        except Exception as e:
            console.print(f"[red]Unexpected error fetching HHAN details: {e}[/red]")
            if meta and meta.get('debug', False):
                console.print_exception()
            elif self.config.get('DEFAULT', {}).get('debug', False):
                console.print_exception()
        
        return hhan_imdb, hhan_tmdb, hhan_name, hhan_torrenthash, hhan_description

    async def get_type_category_id(self, meta: Meta) -> str:
        cat_id = "EXIT"
        category = str(meta.get('category', ''))

        if category == 'MOVIE':
            cat_id = '401'

        if category == 'TV':
            cat_id = '404'
        genres_value = meta.get("genres", "")
        genres = ', '.join(cast(list[str], genres_value)) if isinstance(genres_value, list) else str(genres_value)
        keywords_value = meta.get("keywords", "")
        keywords = ', '.join(cast(list[str], keywords_value)) if isinstance(keywords_value, list) else str(keywords_value)
        if 'documentary' in genres.lower() or 'documentary' in keywords.lower():
            cat_id = '402'

        if 'animation' in genres.lower() or 'animation' in keywords.lower():
            cat_id = '403'

        return cat_id

    async def get_area_id(self, meta: Meta) -> int:

        area_id = 8
        area_map = {  # To do
            "中国大陆": 1, "中国香港": 2, "中国台湾": 3, "美国": 4, "日本": 6, "韩国": 5,
            "印度": 7, "法国": 4, "意大利": 4, "德国": 4, "西班牙": 4, "葡萄牙": 4,
            "英国": 4, "阿根廷": 8, "澳大利亚": 4, "比利时": 4,
            "巴西": 8, "加拿大": 4, "瑞士": 4, "智利": 8,
        }
        ptgen = cast(dict[str, Any], meta.get('ptgen', {}))
        regions_value = ptgen.get("region", [])
        regions = cast(list[str], regions_value) if isinstance(regions_value, list) else []
        for area in area_map:
            if area in regions:
                return area_map[area]
        return area_id

    async def get_type_medium_id(self, meta: Meta) -> str:
        medium_id = "EXIT"
        # 1 = UHD Discs
        if meta.get('is_disc', '') in ("BDMV", "HD DVD"):
            medium_id = '1' if meta['resolution'] == '2160p' else '2'  # BD Discs

        if meta.get('is_disc', '') == "DVD":
            medium_id = '7'

        # 4 = HDTV
        if meta.get('type', '') == "HDTV":
            medium_id = '4'

        # 6 = Encode
        if meta.get('type', '') in ("ENCODE", "WEBRIP"):
            medium_id = '6'

        # 3 = Remux
        if meta.get('type', '') == "REMUX":
            medium_id = '3'

        # 5 = WEB-DL
        if meta.get('type', '') == "WEBDL":
            medium_id = '5'

        return medium_id

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

    async def edit_name(self, meta: Meta) -> str:
        hhan_name = str(meta.get('name', ''))

        remove_list = ['Dubbed', 'Dual-Audio']
        for each in remove_list:
            hhan_name = hhan_name.replace(each, '')

        hhan_name = hhan_name.replace(str(meta.get("aka", '')), '')
        hhan_name = hhan_name.replace('PQ10', 'HDR')

        if meta.get('type') == 'WEBDL' and meta.get('has_encode_settings', False) is True:
            hhan_name = hhan_name.replace('H.264', 'x264')

        return hhan_name

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

        anon = 'no' if meta.get('anon') == 0 and not self.config['TRACKERS'][self.tracker].get('anon', False) else 'yes'

        hhan_name = await self.edit_name(meta)

        mi_path = (
            f"{meta['base_dir']}/tmp/{meta['uuid']}/BD_SUMMARY_00.txt"
            if meta['bdinfo'] is not None
            else f"{meta['base_dir']}/tmp/{meta['uuid']}/MEDIAINFO.txt"
        )
        async with aiofiles.open(mi_path, encoding='utf-8') as mi_dump:
            _ = await mi_dump.read()
        async with aiofiles.open(desc_file, encoding='utf-8') as desc_handle:
            hhan_desc = await desc_handle.read()
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
        data: dict[str, Any] = {
            "name": hhan_name,
            "small_descr": small_descr,
            "descr": hhan_desc,
            "type": await self.get_type_category_id(meta),
            "source_sel": await self.get_type_medium_id(meta),
            "team_sel": await self.get_area_id(meta),
            "uplver": anon,
            "zhongzi": await self.is_zhongzi(meta)
        }
        if meta.get('personalrelease', False) is True:
            data["pr"] = "yes"

        url = "https://hhanclub.net/takeupload.php"

        # Submit
        if meta.get('debug'):
            console.print(url)
            console.print(data)
            meta['tracker_status'][self.tracker]['status_message'] = "Debug mode enabled, not uploading."
            await common.create_torrent_for_upload(meta, f"{self.tracker}" + "_DEBUG", f"{self.tracker}" + "_DEBUG", announce_url="https://fake.tracker")
            return True  # Debug mode - simulated success
        else:
            cookiefile = f"{meta['base_dir']}/data/cookies/HHAN.txt"
            if os.path.exists(cookiefile):
                cookies = await common.parseCookieFile(cookiefile)
                async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
                    up = await client.post(url=url, data=data, files=files)

                    if str(up.url).startswith("https://hhanclub.net/details.php?id="):
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
                        raise UploadException(f"Upload to HHAN Failed: result URL {up.url} ({up.status_code}) was not expected", 'red')  # noqa #F405
        return False

    async def download_new_torrent(self, id: str, torrent_path: str) -> None:
        download_url = f"https://hhanclub.net/download.php?id={id}&passkey={self.passkey}"
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            r = await client.get(url=download_url)
        if r.status_code == 200:
            async with aiofiles.open(torrent_path, "wb") as tor:
                await tor.write(r.content)
        else:
            console.print("[red]There was an issue downloading the new .torrent from hhan")
            console.print(r.text)
