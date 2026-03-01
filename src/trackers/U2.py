# Upload Assistant © 2025 Audionut & wastaken7 — Licensed under UAPL v1.0
import os
import re
from typing import Any, Optional, Union, cast

import httpx
from bs4 import BeautifulSoup

from src.console import console
from src.cookie_auth import CookieValidator
from src.exceptions import *  # noqa E403
from src.trackers.COMMON import COMMON

Meta = dict[str, Any]
Config = dict[str, Any]

# AniDB 链接正则：anidb.net/...aid=123 或 animedb.pl?show=anime&aid=123
ANIDB_AID_RE = re.compile(
    r'anidb\.net[^"\']*[?&]aid=(\d+)|'
    r'animedb\.pl[^"\']*[?&]aid=(\d+)|'
    r'[/?&]aid=(\d+)',
    re.IGNORECASE
)
IDS_MOE_BASE = "https://api.ids.moe"


class U2:

    def __init__(self, config: Config) -> None:
        self.config: Config = config
        self.tracker = 'U2'
        self.source_flag = 'U2'
        self.passkey = str(config['TRACKERS']['U2'].get('passkey', '')).strip()
        self.username = str(config['TRACKERS']['U2'].get('username', '')).strip()
        self.password = str(config['TRACKERS']['U2'].get('password', '')).strip()
        self.rehost_images = bool(config['TRACKERS']['U2'].get('img_rehost', False))
        self.ptgen_api = str(config['TRACKERS']['U2'].get('ptgen_api', '')).strip()
        self.ids_moe_api_key = str(config['TRACKERS']['U2'].get('ids_moe_api_key', '')).strip()

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
        url = "https://u2.dmhy.org"
        cookiefile = f"{meta['base_dir']}/data/cookies/U2.txt"
        if os.path.exists(cookiefile):
            cookies = await common.parseCookieFile(cookiefile)
            async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url=url)

                return resp.text.find('''<a href="#" data-url="logout.php" id="logout-confirm">''') != -1 or resp.text.find('logout') != -1
        else:
            console.print("[bold red]Missing Cookie File. (data/cookies/U2.txt)")
            return False

    async def search_existing(self, meta: Meta, _disctype: str) -> Union[list[str], bool]:
        dupes: list[str] = []
        common = COMMON(config=self.config)
        cookiefile = f"{meta['base_dir']}/data/cookies/U2.txt"
        if not os.path.exists(cookiefile):
            console.print("[bold red]Missing Cookie File. (data/cookies/U2.txt)")
            return False
        cookies = await common.parseCookieFile(cookiefile)
        imdb_id = int(meta.get('imdb_id', 0) or 0)
        imdb = f"tt{meta.get('imdb', '')}" if imdb_id != 0 else ""
        source = await self.get_type_medium_id(meta)
        search_url = f"https://u2.dmhy.org/torrents.php?search={imdb}&incldead=0&search_mode=0&source{source}=1"

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

    async def _resolve_anidb_via_ids_moe(self, anidb_aid: int) -> dict[str, Any]:
        """
        通过 ids.moe 用 AniDB aid 解析 IMDb / TMDB / MAL 等。
        需在 TRACKERS.U2 中配置 ids_moe_api_key（https://ids.moe 申请）。
        返回含 imdb, themoviedb, myanimelist 等键的字典，无则缺省为 None。
        """
        if not self.ids_moe_api_key:
            return {}
        url = f"{IDS_MOE_BASE}/ids/{anidb_aid}"
        params: dict[str, str] = {"p": "anidb"}
        headers: dict[str, str] = {}
        if self.ids_moe_api_key:
            headers["Authorization"] = f"Bearer {self.ids_moe_api_key}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, params=params, headers=headers or None)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            console.print(f"[yellow]ids.moe AniDB 解析失败 (aid={anidb_aid}): {e}[/yellow]")
            return {}
        return cast(dict[str, Any], data)

    async def get_info_from_torrent_id(self, u2_id: Union[int, str], meta: Optional[Meta] = None) -> tuple[Optional[int], Optional[int], Optional[str], Optional[str], Optional[str]]:
        """
        Fetch metadata from U2 torrent details page using torrent ID.
        Returns: (imdb_id, tmdb_id, name, torrenthash, description)
        """
        u2_imdb = u2_tmdb = u2_name = u2_torrenthash = u2_description = None
        common = COMMON(config=self.config)
        base_dir = meta.get('base_dir', '') if meta else ''
        cookiefile = f"{base_dir}/data/cookies/U2.txt"
        
        if not os.path.exists(cookiefile):
            console.print("[bold red]Missing Cookie File. (data/cookies/U2.txt)")
            return u2_imdb, u2_tmdb, u2_name, u2_torrenthash, u2_description
        
        cookies = await common.parseCookieFile(cookiefile)
        url = f"https://u2.dmhy.org/details.php?id={u2_id}"
        
        try:
            async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
                response = await client.get(url)
                
                if response.status_code == 200:
                    # Debug：将页面源码保存到 tmp 目录（与 DESCRIPTION.txt 等同级，不再加 uuid 子目录）
                    if meta and meta.get('debug'):
                        save_dir = f"{meta['base_dir']}/tmp"
                        os.makedirs(save_dir, exist_ok=True)
                        path = f"{save_dir}/u2_page_{u2_id}.html"
                        try:
                            with open(path, 'w', encoding='utf-8', errors='replace') as f:
                                f.write(response.text)
                            console.print(f"[dim]U2: 页面源码已保存到 {path}[/dim]")
                        except OSError as e:
                            console.print(f"[yellow]U2: 保存页面源码失败: {e}[/yellow]")
                    soup = BeautifulSoup(response.text, 'lxml')
                    
                    # Check if logged in - U2 may show login page if not authenticated
                    page_text = response.text.lower()
                    if 'login' in page_text and ('username' in page_text or 'password' in page_text or '未登录' in response.text):
                        console.print(f"[red]U2: Not logged in. Cookie may be expired or invalid. Please update data/cookies/U2.txt[/red]")
                        return u2_imdb, u2_tmdb, u2_name, u2_torrenthash, u2_description
                    
                    # Extract IMDb ID - try multiple selectors
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
                            u2_imdb = int(imdb_match.group(1))
                    
                    # Extract TMDb ID - try multiple selectors
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
                            u2_tmdb = int(tmdb_match.group(2))
                    
                    # Extract Douban ID and URL - try multiple selectors
                    douban_link = soup.select_one('a[href*="movie.douban.com/subject/"]')
                    if not douban_link:
                        for link in soup.find_all('a', href=True):
                            href = link.get('href', '')
                            if 'movie.douban.com/subject/' in href or 'douban.com/subject/' in href:
                                douban_link = link
                                break
                    if douban_link:
                        douban_href = douban_link.get('href', '')
                        # Normalize URL (handle relative URLs)
                        if douban_href.startswith('/'):
                            douban_href = f"https://movie.douban.com{douban_href}"
                        elif not douban_href.startswith('http'):
                            douban_href = f"https://movie.douban.com/subject/{douban_href}"
                        douban_match = re.search(r'/subject/(\d+)', douban_href)
                        if douban_match:
                            douban_id = douban_match.group(1)
                            douban_url = f"https://movie.douban.com/subject/{douban_id}/"
                            if meta:
                                meta['douban_id'] = meta['douban'] = douban_id
                                meta['douban_url'] = douban_url
                            console.print(f"[green]U2: Found Douban ID: {douban_id}, URL: {douban_url}[/green]")
                    # Also search in description text for douban URLs
                    if not douban_link and meta:
                        douban_url_match = re.search(r'https?://movie\.douban\.com/subject/(\d+)', response.text)
                        if douban_url_match:
                            douban_id = douban_url_match.group(1)
                            douban_url = f"https://movie.douban.com/subject/{douban_id}/"
                            if meta:
                                meta['douban_id'] = meta['douban'] = douban_id
                                meta['douban_url'] = douban_url
                            console.print(f"[green]U2: Found Douban ID in page text: {douban_id}, URL: {douban_url}[/green]")
                    
                    # Extract torrent name - try multiple selectors
                    name_elem = soup.select_one('h1, .torrentname, td.torrentname, b.torrentname, table.torrentname')
                    if not name_elem:
                        for row in soup.find_all('tr'):
                            cells = row.find_all('td')
                            for cell in cells:
                                text = cell.get_text(strip=True)
                                if text and len(text) > 10:
                                    name_elem = cell
                                    break
                            if name_elem:
                                break
                    if name_elem:
                        u2_name = name_elem.get_text(strip=True)
                    if not u2_name and soup.find('title'):
                        title_text = soup.find('title').get_text(strip=True)
                        if title_text:
                            u2_name = re.sub(r'\s*[-|]\s*U2.*$', '', title_text, flags=re.IGNORECASE).strip() or title_text
                    
                    # Extract description - try multiple selectors
                    desc_elem = soup.select_one('#desctext, .desctext, td[colspan="2"], .nfo, table.torrentname + table td')
                    if not desc_elem:
                        desc_tables = soup.find_all('table')
                        for table in desc_tables:
                            if 'desctext' in str(table.get('id', '')) or 'desctext' in str(table.get('class', [])):
                                desc_elem = table
                                break
                    if not desc_elem:
                        for tag in soup.find_all(['td', 'div'], class_=re.compile(r'desc|nfo|content|detail', re.I)):
                            if tag.get_text(strip=True) and len(tag.get_text(strip=True)) > 100:
                                desc_elem = tag
                                break
                    if desc_elem:
                        u2_description = str(desc_elem)
                    
                    # Extract torrent hash (if available in page)
                    hash_elem = soup.select_one('input[name="hash"], code, .hash, font[color="red"]')
                    if hash_elem:
                        hash_text = hash_elem.get_text(strip=True)
                        if len(hash_text) == 40:  # SHA1 hash length
                            u2_torrenthash = hash_text

                    # U2 种子常仅含 AniDB：从页面/描述解析 AniDB aid，用 ids.moe 换 IMDb/TMDB（馒头与 ptgen 需要）
                    anidb_aid: Optional[int] = None
                    for m in ANIDB_AID_RE.finditer(response.text):
                        g = m.group(1) or m.group(2) or m.group(3)
                        if g and g.isdigit():
                            anidb_aid = int(g)
                            break
                    if meta and anidb_aid is not None:
                        meta['anidb_aid'] = anidb_aid
                    if anidb_aid is not None and not self.ids_moe_api_key:
                        console.print("[yellow]U2: 页面含 AniDB 链接但未配置 ids_moe_api_key，无法解析 IMDb/TMDB。请在 TRACKERS.U2 中配置 ids_moe_api_key（https://ids.moe 申请）[/yellow]")
                    if (u2_imdb is None or u2_tmdb is None) and anidb_aid is not None and self.ids_moe_api_key:
                        ids_data = await self._resolve_anidb_via_ids_moe(anidb_aid)
                        if ids_data:
                            if u2_imdb is None and ids_data.get('imdb'):
                                raw = str(ids_data['imdb']).strip().lstrip('t')
                                if raw.isdigit():
                                    u2_imdb = int(raw)
                                    console.print(f"[green]U2: 从 AniDB aid={anidb_aid} 解析到 IMDb: tt{u2_imdb}[/green]")
                            if u2_tmdb is None and ids_data.get('themoviedb'):
                                try:
                                    u2_tmdb = int(ids_data['themoviedb'])
                                    console.print(f"[green]U2: 从 AniDB aid={anidb_aid} 解析到 TMDb: {u2_tmdb}[/green]")
                                except (TypeError, ValueError):
                                    pass
                            if meta and ids_data.get('myanimelist') and not meta.get('mal_id'):
                                try:
                                    meta['mal_id'] = int(ids_data['myanimelist'])
                                except (TypeError, ValueError):
                                    pass
                    
                else:
                    console.print(f"[yellow]Failed to fetch U2 details page. Status: {response.status_code}[/yellow]")
                    
        except httpx.RequestError as e:
            console.print(f"[red]Request error fetching U2 details: {e}[/red]")
        except Exception as e:
            console.print(f"[red]Unexpected error fetching U2 details: {e}[/red]")
            if meta and meta.get('debug', False):
                console.print_exception()
            elif self.config.get('DEFAULT', {}).get('debug', False):
                console.print_exception()
        
        return u2_imdb, u2_tmdb, u2_name, u2_torrenthash, u2_description

    async def get_type_category_id(self, meta: Meta) -> str:
        cat_id = "0"  # Default
        category = str(meta.get('category', ''))

        if category == 'MOVIE':
            cat_id = '401'  # 电影

        if category == 'TV':
            cat_id = '404'  # 电视剧
        
        genres_value = meta.get("genres", "")
        genres = ', '.join(cast(list[str], genres_value)) if isinstance(genres_value, list) else str(genres_value)
        keywords_value = meta.get("keywords", "")
        keywords = ', '.join(cast(list[str], keywords_value)) if isinstance(keywords_value, list) else str(keywords_value)
        
        # Check for animation
        if 'animation' in genres.lower() or 'animation' in keywords.lower() or 'anime' in genres.lower():
            cat_id = '403'  # 动画
        
        # Check for variety shows/reality TV
        if 'variety' in genres.lower() or 'reality' in genres.lower() or 'talk show' in genres.lower():
            cat_id = '405'  # 综艺
        
        # Check for documentary
        if 'documentary' in genres.lower() or 'documentary' in keywords.lower():
            cat_id = '402'  # 纪录片

        return cat_id

    async def get_area_id(self, meta: Meta) -> int:
        area_id = 8
        area_map = {
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
