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


class CHD:

    def __init__(self, config: Config) -> None:
        self.config: Config = config
        self.tracker = 'CHD'
        self.source_flag = 'CHD'
        self.passkey = str(config['TRACKERS']['CHD'].get('passkey', '')).strip()
        self.username = str(config['TRACKERS']['CHD'].get('username', '')).strip()
        self.password = str(config['TRACKERS']['CHD'].get('password', '')).strip()
        self.rehost_images = bool(config['TRACKERS']['CHD'].get('img_rehost', False))
        self.ptgen_api = str(config['TRACKERS']['CHD'].get('ptgen_api', '')).strip()

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
        url = "https://ptchdbits.co"
        cookiefile = f"{meta['base_dir']}/data/cookies/CHD.txt"
        if os.path.exists(cookiefile):
            cookies = await common.parseCookieFile(cookiefile)
            async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url=url)

                return resp.text.find('''<a href="#" data-url="logout.php" id="logout-confirm">''') != -1 or resp.text.find('logout') != -1
        else:
            console.print("[bold red]Missing Cookie File. (data/cookies/CHD.txt)")
            return False

    async def search_existing(self, meta: Meta, _disctype: str) -> Union[list[str], bool]:
        dupes: list[str] = []
        common = COMMON(config=self.config)
        cookiefile = f"{meta['base_dir']}/data/cookies/CHD.txt"
        if not os.path.exists(cookiefile):
            console.print("[bold red]Missing Cookie File. (data/cookies/CHD.txt)")
            return False
        cookies = await common.parseCookieFile(cookiefile)
        imdb_id = int(meta.get('imdb_id', 0) or 0)
        imdb = f"tt{meta.get('imdb', '')}" if imdb_id != 0 else ""
        source = await self.get_type_medium_id(meta)
        search_url = f"https://ptchdbits.co/torrents.php?search={imdb}&incldead=0&search_mode=0&source{source}=1"

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

    async def get_info_from_torrent_id(self, chd_id: Union[int, str], meta: Optional[Meta] = None) -> tuple[Optional[int], Optional[int], Optional[str], Optional[str], Optional[str]]:
        """
        Fetch metadata from CHD torrent details page using torrent ID.
        Returns: (imdb_id, tmdb_id, name, torrenthash, description)
        """
        chd_imdb = chd_tmdb = chd_name = chd_torrenthash = chd_description = None
        common = COMMON(config=self.config)
        base_dir = meta.get('base_dir', '') if meta else ''
        cookiefile = f"{base_dir}/data/cookies/CHD.txt"
        
        if not os.path.exists(cookiefile):
            console.print("[bold red]Missing Cookie File. (data/cookies/CHD.txt)[/bold red]")
            return chd_imdb, chd_tmdb, chd_name, chd_torrenthash, chd_description
        
        cookies = await common.parseCookieFile(cookiefile)
        url = f"https://ptchdbits.co/details.php?id={chd_id}"
        
        try:
            async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
                response = await client.get(url)
                
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'lxml')
                    
                    # Check if logged in - CHD shows "未登录!" or login page if not authenticated
                    page_text = response.text.lower()
                    if '未登录' in response.text or ('login' in page_text and ('username' in page_text or 'password' in page_text)):
                        console.print(f"[red]CHD: Not logged in. Cookie may be expired or invalid. Please update data/cookies/CHD.txt[/red]")
                        return chd_imdb, chd_tmdb, chd_name, chd_torrenthash, chd_description
                    
                    # Extract IMDb ID - try multiple selectors
                    imdb_link = soup.select_one('a[href*="imdb.com/title/tt"], a[href*="imdb.com/title/tt"]')
                    if not imdb_link:
                        # Try finding in text content
                        for link in soup.find_all('a', href=True):
                            href = link.get('href', '')
                            if 'imdb.com/title/tt' in href:
                                imdb_link = link
                                break
                    if imdb_link:
                        imdb_href = imdb_link.get('href', '')
                        imdb_match = re.search(r'tt(\d+)', imdb_href)
                        if imdb_match:
                            chd_imdb = int(imdb_match.group(1))
                    
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
                            chd_tmdb = int(tmdb_match.group(2))
                    
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
                        if douban_match and meta:
                            douban_id = douban_match.group(1)
                            douban_url = f"https://movie.douban.com/subject/{douban_id}/"
                            meta['douban_id'] = meta['douban'] = douban_id
                            meta['douban_url'] = douban_url
                            console.print(f"[green]CHD: Found Douban ID: {douban_id}, URL: {douban_url}[/green]")
                    # Also search in description text for douban URLs
                    if not douban_link and meta:
                        douban_url_match = re.search(r'https?://movie\.douban\.com/subject/(\d+)', response.text)
                        if douban_url_match:
                            douban_id = douban_url_match.group(1)
                            douban_url = f"https://movie.douban.com/subject/{douban_id}/"
                            meta['douban_id'] = meta['douban'] = douban_id
                            meta['douban_url'] = douban_url
                            console.print(f"[green]CHD: Found Douban ID in page text: {douban_id}, URL: {douban_url}[/green]")
                    
                    # Extract torrent name - try multiple selectors
                    name_elem = soup.select_one('h1, .torrentname, td.torrentname, b.torrentname, table.torrentname')
                    if not name_elem:
                        # Try finding in table rows
                        for row in soup.find_all('tr'):
                            cells = row.find_all('td')
                            for cell in cells:
                                text = cell.get_text(strip=True)
                                if text and len(text) > 10 and '未登录' not in text:
                                    name_elem = cell
                                    break
                            if name_elem:
                                break
                    if name_elem:
                        chd_name = name_elem.get_text(strip=True)
                        # Filter out login-related text
                        if '未登录' in chd_name or chd_name == '未登录!':
                            chd_name = None
                            console.print(f"[yellow]CHD: Detected login page, cookie may be invalid[/yellow]")
                    
                    # Extract description - try multiple selectors
                    desc_elem = soup.select_one('#desctext, .desctext, td[colspan="2"], .nfo, table.torrentname + table td')
                    if not desc_elem:
                        # Try finding description table
                        desc_tables = soup.find_all('table')
                        for table in desc_tables:
                            if 'desctext' in str(table.get('id', '')) or 'desctext' in str(table.get('class', [])):
                                desc_elem = table
                                break
                    if desc_elem:
                        desc_text = str(desc_elem)
                        # Check if description contains login page content
                        if '未登录' not in desc_text:
                            chd_description = desc_text
                    
                    # Extract torrent hash (if available in page)
                    hash_elem = soup.select_one('input[name="hash"], code, .hash, font[color="red"]')
                    if hash_elem:
                        hash_text = hash_elem.get_text(strip=True)
                        if len(hash_text) == 40:  # SHA1 hash length
                            chd_torrenthash = hash_text
                    
                else:
                    console.print(f"[yellow]Failed to fetch CHD details page. Status: {response.status_code}[/yellow]")
                    
        except httpx.RequestError as e:
            console.print(f"[red]Request error fetching CHD details: {e}[/red]")
        except Exception as e:
            console.print(f"[red]Unexpected error fetching CHD details: {e}[/red]")
            if meta and meta.get('debug', False):
                console.print_exception()
            elif self.config.get('DEFAULT', {}).get('debug', False):
                console.print_exception()
        
        return chd_imdb, chd_tmdb, chd_name, chd_torrenthash, chd_description

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
