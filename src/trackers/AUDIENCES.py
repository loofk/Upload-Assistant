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


class AUDIENCES:

    def __init__(self, config: Config) -> None:
        self.config: Config = config
        self.tracker = 'AUDIENCES'
        self.source_flag = 'AUDIENCES'
        self.passkey = str(config['TRACKERS']['AUDIENCES'].get('passkey', '')).strip()
        self.username = str(config['TRACKERS']['AUDIENCES'].get('username', '')).strip()
        self.password = str(config['TRACKERS']['AUDIENCES'].get('password', '')).strip()
        self.rehost_images = bool(config['TRACKERS']['AUDIENCES'].get('img_rehost', False))
        self.ptgen_api = str(config['TRACKERS']['AUDIENCES'].get('ptgen_api', '')).strip()

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
        url = "https://audiences.me"
        cookiefile = f"{meta['base_dir']}/data/cookies/AUDIENCES.txt"
        if os.path.exists(cookiefile):
            cookies = await common.parseCookieFile(cookiefile)
            async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url=url)

                return resp.text.find('''<a href="#" data-url="logout.php" id="logout-confirm">''') != -1
        else:
            console.print("[bold red]Missing Cookie File. (data/cookies/AUDIENCES.txt)")
            return False

    async def search_existing(self, meta: Meta, _disctype: str) -> Union[list[str], bool]:
        dupes: list[str] = []
        common = COMMON(config=self.config)
        cookiefile = f"{meta['base_dir']}/data/cookies/AUDIENCES.txt"
        if not os.path.exists(cookiefile):
            console.print("[bold red]Missing Cookie File. (data/cookies/AUDIENCES.txt)")
            return False
        cookies = await common.parseCookieFile(cookiefile)
        imdb_id = int(meta.get('imdb_id', 0) or 0)
        imdb = f"tt{meta.get('imdb', '')}" if imdb_id != 0 else ""
        source = await self.get_type_medium_id(meta)
        search_url = f"https://audiences.me/torrents.php?search={imdb}&incldead=0&search_mode=0&source{source}=1"

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

    async def get_type_category_id(self, meta: Meta) -> str:
        cat_id = "0"  # Default to "请选择"
        category = str(meta.get('category', ''))

        if category == 'MOVIE':
            cat_id = '401'  # 电影

        if category == 'TV':
            cat_id = '402'  # 剧集
        
        genres_value = meta.get("genres", "")
        genres = ', '.join(cast(list[str], genres_value)) if isinstance(genres_value, list) else str(genres_value)
        keywords_value = meta.get("keywords", "")
        keywords = ', '.join(cast(list[str], keywords_value)) if isinstance(keywords_value, list) else str(keywords_value)
        
        # Check for variety shows/reality TV
        if 'variety' in genres.lower() or 'reality' in genres.lower() or 'talk show' in genres.lower():
            cat_id = '403'  # 综艺
        
        # Check for documentary
        if 'documentary' in genres.lower() or 'documentary' in keywords.lower():
            cat_id = '406'  # 纪录片

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

    async def get_medium_sel(self, meta: Meta) -> str:
        """Get medium selection ID for AUDIENCES form"""
        # 12 = UHD Blu-ray 原盘, 13 = UHD Blu-ray DIY, 1 = Blu-ray 原盘, 14 = Blu-ray DIY
        # 3 = REMUX, 15 = Encode, 5 = HDTV, 10 = WEB-DL, 2 = DVD 原盘
        medium_id = "0"  # Default to "请选择"
        
        if meta.get('is_disc', '') == "BDMV":
            if meta.get('resolution', '') == '2160p':
                # Check if DIY (has custom encoding settings)
                if meta.get('has_encode_settings', False):
                    medium_id = '13'  # UHD Blu-ray DIY
                else:
                    medium_id = '12'  # UHD Blu-ray 原盘
            else:
                # Check if DIY
                if meta.get('has_encode_settings', False):
                    medium_id = '14'  # Blu-ray DIY
                else:
                    medium_id = '1'  # Blu-ray 原盘
        
        if meta.get('is_disc', '') == "DVD":
            medium_id = '2'  # DVD 原盘
        
        if meta.get('type', '') == "REMUX":
            medium_id = '3'  # REMUX
        
        if meta.get('type', '') == "HDTV":
            medium_id = '5'  # HDTV
        
        if meta.get('type', '') == "WEBDL":
            medium_id = '10'  # WEB-DL
        
        if meta.get('type', '') in ("ENCODE", "WEBRIP"):
            medium_id = '15'  # Encode
        
        return medium_id

    async def get_codec_sel(self, meta: Meta) -> str:
        """Get codec selection ID for AUDIENCES form"""
        # 6 = H.265(HEVC), 1 = H.264(AVC), 2 = VC-1, 4 = MPEG-2, 7 = AV1, 5 = Other
        codec_id = "0"  # Default to "请选择"
        
        mi = cast(dict[str, Any], meta.get('mediainfo', {}))
        video_tracks = []
        if mi:
            media = cast(dict[str, Any], mi.get('media', {}))
            tracks = cast(list[dict[str, Any]], media.get('track', []))
            video_tracks = [t for t in tracks if t.get('@type') == 'Video']
        
        if video_tracks:
            codec = str(video_tracks[0].get('Format', '')).upper()
            if 'HEVC' in codec or 'H.265' in codec or 'X265' in codec:
                codec_id = '6'  # H.265(HEVC)
            elif 'AVC' in codec or 'H.264' in codec or 'X264' in codec:
                codec_id = '1'  # H.264(AVC)
            elif 'VC-1' in codec:
                codec_id = '2'  # VC-1
            elif 'MPEG-2' in codec or 'MPEG2' in codec:
                codec_id = '4'  # MPEG-2
            elif 'AV1' in codec:
                codec_id = '7'  # AV1
            else:
                codec_id = '5'  # Other
        
        return codec_id

    async def get_audiocodec_sel(self, meta: Meta) -> str:
        """Get audio codec selection ID for AUDIENCES form"""
        # 25 = DTS:X, 26 = TrueHD Atmos, 19 = DTS-HD MA, 20 = TrueHD, 21 = LPCM
        # 3 = DTS, 18 = DD/AC3, 27 = OPUS, 6 = AAC, 1 = FLAC, 2 = APE, 22 = WAV, 23 = MP3, 24 = M4A, 7 = Other
        audio_id = "0"  # Default to "请选择"
        
        mi = cast(dict[str, Any], meta.get('mediainfo', {}))
        audio_tracks = []
        if mi:
            media = cast(dict[str, Any], mi.get('media', {}))
            tracks = cast(list[dict[str, Any]], media.get('track', []))
            audio_tracks = [t for t in tracks if t.get('@type') == 'Audio']
        
        if audio_tracks:
            codec = str(audio_tracks[0].get('Format', '')).upper()
            format_profile = str(audio_tracks[0].get('Format_Profile', '')).upper()
            
            if 'DTS:X' in format_profile or 'DTSX' in format_profile:
                audio_id = '25'  # DTS:X
            elif 'ATMOS' in format_profile or 'TRUEHD ATMOS' in codec:
                audio_id = '26'  # TrueHD Atmos
            elif 'DTS-HD' in codec or 'DTSHD' in codec:
                audio_id = '19'  # DTS-HD MA
            elif 'TRUEHD' in codec:
                audio_id = '20'  # TrueHD
            elif 'LPCM' in codec or 'PCM' in codec:
                audio_id = '21'  # LPCM
            elif 'DTS' in codec:
                audio_id = '3'  # DTS
            elif 'AC3' in codec or 'DD' in codec or 'DOLBY DIGITAL' in codec:
                audio_id = '18'  # DD/AC3
            elif 'OPUS' in codec:
                audio_id = '27'  # OPUS
            elif 'AAC' in codec:
                audio_id = '6'  # AAC
            elif 'FLAC' in codec:
                audio_id = '1'  # FLAC
            elif 'APE' in codec:
                audio_id = '2'  # APE
            elif 'WAV' in codec:
                audio_id = '22'  # WAV
            elif 'MP3' in codec:
                audio_id = '23'  # MP3
            elif 'M4A' in codec:
                audio_id = '24'  # M4A
            else:
                audio_id = '7'  # Other
        
        return audio_id

    async def get_standard_sel(self, meta: Meta) -> str:
        """Get resolution/standard selection ID for AUDIENCES form"""
        # 10 = 8K, 5 = 4K, 1 = 1080p, 2 = 1080i, 3 = 720p, 4 = SD, 11 = None
        resolution = str(meta.get('resolution', '')).lower()
        
        if '8k' in resolution or '7680' in resolution:
            return '10'  # 8K
        elif '4k' in resolution or '2160p' in resolution or '2160i' in resolution:
            return '5'  # 4K
        elif '1080p' in resolution:
            return '1'  # 1080p
        elif '1080i' in resolution:
            return '2'  # 1080i
        elif '720p' in resolution or '720i' in resolution:
            return '3'  # 720p
        elif '480p' in resolution or '480i' in resolution or '576p' in resolution or '576i' in resolution:
            return '4'  # SD
        else:
            return '11'  # None

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
        audiences_name = str(meta.get('name', ''))

        remove_list = ['Dubbed', 'Dual-Audio']
        for each in remove_list:
            audiences_name = audiences_name.replace(each, '')

        audiences_name = audiences_name.replace(str(meta.get("aka", '')), '')
        audiences_name = audiences_name.replace('PQ10', 'HDR')

        if meta.get('type') == 'WEBDL' and meta.get('has_encode_settings', False) is True:
            audiences_name = audiences_name.replace('H.264', 'x264')

        return audiences_name

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

        # Check anonymous upload (checkbox format: "yes" if checked, omitted if not)
        anon = None
        if meta.get('anon') == 1 or self.config['TRACKERS'][self.tracker].get('anon', False):
            anon = 'yes'

        audiences_name = await self.edit_name(meta)

        async with aiofiles.open(desc_file, encoding='utf-8') as desc_handle:
            audiences_desc = await desc_handle.read()
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
        
        # Build form data according to AUDIENCES form structure
        data: dict[str, Any] = {
            "name": audiences_name,
            "small_descr": small_descr,
            "descr": audiences_desc,
            "type": await self.get_type_category_id(meta),
            "medium_sel": await self.get_medium_sel(meta),
            "codec_sel": await self.get_codec_sel(meta),
            "audiocodec_sel": await self.get_audiocodec_sel(meta),
            "standard_sel": await self.get_standard_sel(meta),
        }
        
        # Add IMDb URL if available
        imdb_id = int(meta.get('imdb_id', 0) or 0)
        if imdb_id != 0:
            data["url"] = f"https://www.imdb.com/title/tt{meta.get('imdb', '')}/"
        
        # Add anonymous upload checkbox if needed
        if anon:
            data["uplver"] = anon
        
        # Add tags based on metadata
        tags = []
        # Check for Chinese subtitles
        chinese_sub = await self.is_zhongzi(meta)
        if chinese_sub == 'yes':
            tags.append('zz')  # 中字
        
        # Check for animation
        genres_value = meta.get("genres", "")
        genres = ', '.join(cast(list[str], genres_value)) if isinstance(genres_value, list) else str(genres_value)
        if 'animation' in genres.lower() or 'anime' in genres.lower():
            tags.append('dh')  # 动画
        
        # Check for completed series
        if meta.get('tv_pack', 0) == 1:
            tags.append('wj')  # 完结
        
        # Add tags if any
        if tags:
            data["tags[]"] = tags

        url = "https://audiences.me/takeupload.php"

        # Submit
        if meta.get('debug'):
            console.print(url)
            console.print(data)
            meta['tracker_status'][self.tracker]['status_message'] = "Debug mode enabled, not uploading."
            await common.create_torrent_for_upload(meta, f"{self.tracker}" + "_DEBUG", f"{self.tracker}" + "_DEBUG", announce_url="https://fake.tracker")
            return True  # Debug mode - simulated success
        else:
            cookiefile = f"{meta['base_dir']}/data/cookies/AUDIENCES.txt"
            if os.path.exists(cookiefile):
                cookies = await common.parseCookieFile(cookiefile)
                async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
                    up = await client.post(url=url, data=data, files=files)

                    if str(up.url).startswith("https://audiences.me/details.php?id="):
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
                        raise UploadException(f"Upload to AUDIENCES Failed: result URL {up.url} ({up.status_code}) was not expected", 'red')  # noqa #F405
        return False

    async def download_new_torrent(self, id: str, torrent_path: str) -> None:
        download_url = f"https://audiences.me/download.php?id={id}&passkey={self.passkey}"
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            r = await client.get(url=download_url)
        if r.status_code == 200:
            async with aiofiles.open(torrent_path, "wb") as tor:
                await tor.write(r.content)
        else:
            console.print("[red]There was an issue downloading the new .torrent from audiences")
            console.print(r.text)
