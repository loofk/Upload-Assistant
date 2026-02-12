# Upload Assistant © 2025 Audionut & wastaken7 — Licensed under UAPL v1.0
import os
import re
from typing import Any, Optional, Union, cast

import aiofiles
import httpx
from bs4 import BeautifulSoup
from unidecode import unidecode

from src.console import console
from src.exceptions import UploadException  # noqa E403
from src.trackers.COMMON import COMMON

Meta = dict[str, Any]
Config = dict[str, Any]


class MTEAM:

    def __init__(self, config: Config) -> None:
        self.config: Config = config
        self.tracker = 'MTEAM'
        self.source_flag = 'MTEAM'
        self.api_key = str(config['TRACKERS']['MTEAM'].get('api_key', '')).strip()
        self.uid = int(config['TRACKERS']['MTEAM'].get('uid', 0) or 0)
        self.ptgen_api = str(config['TRACKERS']['MTEAM'].get('ptgen_api', '')).strip()
        self.ptgen_retry = 3
        self.signature: Optional[str] = None
        self.banned_groups: list[str] = []
        
        # Create session with API key header
        self.session = httpx.AsyncClient(headers={
            'User-Agent': 'Upload Assistant',
            'accept': 'application/json',
            'x-api-key': self.api_key,
        }, timeout=60.0)

    async def validate_credentials(self, meta: Meta) -> bool:
        """Validate API key by making a test request"""
        if not self.api_key:
            console.print('[red]Failed to validate API key. Please set api_key in config.')
            return False
        
        try:
            # Test API key by making a request to user profile endpoint
            # According to API docs: POST /api/member/profile with uid parameter
            url = "https://api.m-team.cc/api/member/profile"
            headers = {
                'x-api-key': self.api_key,
                'accept': 'application/json',
                'Content-Type': 'application/x-www-form-urlencoded',
            }
            # uid is required, get from config
            data = {'uid': self.uid}
            response = await self.session.post(url, data=data, headers=headers)
            if response.status_code == 200:
                try:
                    response_json = response.json()
                    # API response format: {"code": 0 or "0", "message": "", "data": {}}
                    # Handle both string and integer code values
                    code = response_json.get('code')
                    if code == 0 or code == "0" or str(code) == "0":
                        return True
                    else:
                        console.print(f'[yellow]API validation returned code {response_json.get("code")}. Proceeding anyway.')
                        return True
                except Exception:
                    return True
            elif response.status_code == 401:
                console.print('[red]Invalid API key. Please check your token.')
                return False
            else:
                console.print(f'[yellow]API validation returned status {response.status_code}. Proceeding anyway.')
                return True
        except Exception as e:
            console.print(f'[yellow]API validation error: {e}. Proceeding anyway.')
            return True

    async def search_existing(self, meta: Meta, _disctype: str) -> Union[list[str], bool]:
        """Search for existing torrents using API"""
        dupes: list[str] = []
        imdb_id = int(meta.get('imdb_id', 0) or 0)
        if imdb_id == 0:
            return dupes
        
        imdb = f"tt{meta.get('imdb', '')}"
        # Try API search endpoint
        search_url = f"https://api.m-team.cc/api/torrent/search"
        
        try:
            data = {'imdb': imdb}
            headers = {
                'x-api-key': self.api_key,
                'accept': 'application/json',
            }
            response = await self.session.post(search_url, data=data, headers=headers)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    # Adjust based on actual API response structure
                    if isinstance(data, dict):
                        torrents = data.get('data', []) or data.get('torrents', [])
                    elif isinstance(data, list):
                        torrents = data
                    else:
                        torrents = []
                    
                    for torrent in torrents:
                        if isinstance(torrent, dict):
                            name = torrent.get('name') or torrent.get('title', '')
                            if name:
                                dupes.append(str(name))
                except Exception:
                    # Fallback to HTML parsing if API returns HTML
                    soup = BeautifulSoup(response.text, 'lxml')
                    rows = soup.select('a[href*="/torrents/"]')
                    for row in rows:
                        title = row.get_text(strip=True)
                        if title:
                            dupes.append(title)
        except httpx.TimeoutException:
            console.print("[bold red]Request timed out while searching for existing torrents.")
        except httpx.RequestError as e:
            console.print(f"[bold red]An error occurred while making the request: {e}")
        except Exception as e:
            console.print(f"[bold red]Unexpected error: {e}")
            console.print_exception()

        return dupes

    async def get_category_id(self, meta: Meta) -> Optional[int]:
        """Get category ID for MTEAM form (returns integer ID)"""
        category = str(meta.get('category', ''))
        resolution = str(meta.get('resolution', '')).lower()
        media_type = str(meta.get('type', ''))
        is_disc = meta.get('is_disc', '')
        
        # Map category strings to integer IDs - these may need adjustment based on actual API
        # 電影 (Movie) categories
        if category == 'MOVIE':
            # 電影/Blu-Ray
            if is_disc == 'BDMV':
                return 421
            # 電影/DVDiSo
            elif is_disc == 'DVD':
                return 420
            # 電影/Remux
            elif media_type == 'REMUX':
                return 439
            # 電影/HD (1080p, 720p, 4K)
            elif resolution in ('1080p', '720p', '2160p', '4k'):
                return 419
            # 電影/SD (default for movies)
            else:
                return 401
        
        # 影劇/綜藝 (TV Series/Variety) categories
        elif category == 'TV':
            # Check for variety shows
            genres_value = meta.get("genres", "")
            genres = ', '.join(cast(list[str], genres_value)) if isinstance(genres_value, list) else str(genres_value)
            keywords_value = meta.get("keywords", "")
            keywords = ', '.join(cast(list[str], keywords_value)) if isinstance(keywords_value, list) else str(keywords_value)
            
            is_variety = 'variety' in genres.lower() or 'reality' in genres.lower() or 'talk show' in genres.lower()
            
            if is_variety:
                # 影劇/綜藝/BD
                if is_disc == 'BDMV':
                    return 438
                # 影劇/綜藝/DVDiSo
                elif is_disc == 'DVD':
                    return 435
                # 影劇/綜藝/HD
                elif resolution in ('1080p', '720p', '2160p', '4k'):
                    return 402
                # 影劇/綜藝/SD
                else:
                    return 403
            else:
                # For regular TV series, use HD category
                if is_disc == 'BDMV':
                    return 438  # 影劇/綜藝/BD
                elif is_disc == 'DVD':
                    return 435  # 影劇/綜藝/DVDiSo
                elif resolution in ('1080p', '720p', '2160p', '4k'):
                    return 402  # 影劇/綜藝/HD
                else:
                    return 403  # 影劇/綜藝/SD
        
        # 紀錄 (Documentary)
        genres_value = meta.get("genres", "")
        genres = ', '.join(cast(list[str], genres_value)) if isinstance(genres_value, list) else str(genres_value)
        keywords_value = meta.get("keywords", "")
        keywords = ', '.join(cast(list[str], keywords_value)) if isinstance(keywords_value, list) else str(keywords_value)
        
        if 'documentary' in genres.lower() or 'documentary' in keywords.lower():
            return 404  # 紀錄
        
        # 動畫 (Animation)
        if 'animation' in genres.lower() or 'animation' in keywords.lower() or meta.get('anime', False):
            return 405  # 動畫
        
        # 運動 (Sports)
        if 'sport' in genres.lower() or 'sports' in genres.lower():
            return 407  # 運動
        
        # Default to Misc
        return 409  # Misc(其他)

    async def get_standard_id(self, meta: Meta) -> Optional[int]:
        """Get resolution/standard ID for MTEAM form (returns integer ID)
        Based on standardList.json: 1=1080p, 2=1080i, 3=720p, 5=SD, 6=4K, 7=8K
        """
        resolution = str(meta.get('resolution', '')).lower()
        
        # Map resolution strings to integer IDs based on standardList.json
        if '8k' in resolution or '7680' in resolution:
            return 7  # 8K
        elif '4k' in resolution or '2160p' in resolution or '2160i' in resolution:
            return 6  # 4K
        elif '1080p' in resolution:
            return 1  # 1080p
        elif '1080i' in resolution:
            return 2  # 1080i
        elif '720p' in resolution or '720i' in resolution:
            return 3  # 720p
        elif '480p' in resolution or '480i' in resolution or '576p' in resolution or '576i' in resolution:
            return 5  # SD
        else:
            return None

    async def get_video_codec_id(self, meta: Meta) -> Optional[int]:
        """Get video codec ID for MTEAM form (returns integer ID)
        Based on videoCodecList.json: 1=H.264, 16=H.265/HEVC, 2=VC-1, 4=MPEG-2, 3=Xvid, 19=AV1, 21=VP8/9, 22=AVS
        """
        mi = cast(dict[str, Any], meta.get('mediainfo', {}))
        video_tracks = []
        if mi:
            media = cast(dict[str, Any], mi.get('media', {}))
            tracks = cast(list[dict[str, Any]], media.get('track', []))
            video_tracks = [t for t in tracks if t.get('@type') == 'Video']
        
        if video_tracks:
            codec = str(video_tracks[0].get('Format', '')).upper()
            # Map codec strings to integer IDs based on videoCodecList.json
            if 'HEVC' in codec or 'H.265' in codec or 'X265' in codec:
                return 16  # H.265(x265/HEVC)
            elif 'AVC' in codec or 'H.264' in codec or 'X264' in codec:
                return 1  # H.264(x264/AVC)
            elif 'VC-1' in codec:
                return 2  # VC-1
            elif 'MPEG-2' in codec or 'MPEG2' in codec:
                return 4  # MPEG-2
            elif 'XVID' in codec or 'XVID' in codec:
                return 3  # Xvid
            elif 'AV1' in codec:
                return 19  # AV1
            elif 'VP8' in codec or 'VP9' in codec:
                return 21  # VP8/9
            elif 'AVS' in codec:
                return 22  # AVS
        
        return None

    async def get_audio_codec_id(self, meta: Meta) -> Optional[int]:
        """Get audio codec ID for MTEAM form (returns integer ID)
        Based on audioCodecList.json: 6=AAC, 8=AC3, 3=DTS, 11=DTS-HD MA, 12=E-AC3, 13=E-AC3 Atoms,
        9=TrueHD, 10=TrueHD Atmos, 14=LPCM/PCM, 15=WAV, 1=FLAC, 2=APE, 4=MP2/3, 5=OGG, 7=Other
        """
        mi = cast(dict[str, Any], meta.get('mediainfo', {}))
        audio_tracks = []
        if mi:
            media = cast(dict[str, Any], mi.get('media', {}))
            tracks = cast(list[dict[str, Any]], media.get('track', []))
            audio_tracks = [t for t in tracks if t.get('@type') == 'Audio']
        
        if audio_tracks:
            codec = str(audio_tracks[0].get('Format', '')).upper()
            format_profile = str(audio_tracks[0].get('Format_Profile', '')).upper()
            
            # Map codec strings to integer IDs based on audioCodecList.json
            # Check for more specific formats first
            if 'E-AC3' in codec and ('ATMOS' in format_profile or 'ATMOS' in codec):
                return 13  # E-AC3 Atoms(DDP Atoms)
            elif 'E-AC3' in codec or 'DDP' in codec:
                return 12  # E-AC3(DDP)
            elif 'ATMOS' in format_profile or 'TRUEHD ATMOS' in codec:
                return 10  # TrueHD Atmos
            elif 'TRUEHD' in codec:
                return 9  # TrueHD
            elif 'DTS-HD' in codec or 'DTSHD' in codec or 'DTS-HD MA' in codec:
                return 11  # DTS-HD MA
            elif 'DTS:X' in format_profile or 'DTSX' in format_profile:
                return 3  # DTS (DTS:X may map to DTS, need to verify)
            elif 'DTS' in codec:
                return 3  # DTS
            elif 'LPCM' in codec or 'PCM' in codec:
                return 14  # LPCM/PCM
            elif 'WAV' in codec:
                return 15  # WAV
            elif 'FLAC' in codec:
                return 1  # FLAC
            elif 'APE' in codec:
                return 2  # APE
            elif 'MP2' in codec or 'MP3' in codec:
                return 4  # MP2/3
            elif 'OGG' in codec or 'VORBIS' in codec:
                return 5  # OGG
            elif 'AC3' in codec or 'DD' in codec or 'DOLBY DIGITAL' in codec:
                return 8  # AC3(DD)
            elif 'AAC' in codec:
                return 6  # AAC
        
        return None

    async def get_countries(self, meta: Meta) -> list[str]:
        """Get country/region IDs for MTEAM form (multi-select)
        Returns list of country ID strings based on countryList.json
        """
        countries = []
        ptgen = cast(dict[str, Any], meta.get('ptgen', {}))
        regions_value = ptgen.get("region", [])
        regions = cast(list[str], regions_value) if isinstance(regions_value, list) else []
        
        # Map Chinese region names to MTEAM country IDs based on countryList.json
        # ID mappings: 2=United States, 6=France, 7=Germany, 8=中国, 9=Italy, 12=United Kingdom,
        # 17=Japan, 20=Australia, 30=South Korea, 70=India, etc.
        country_map = {
            "中国大陆": "8",  # 中国
            "中国": "8",
            "中国香港": "8",  # May need separate ID if Hong Kong is listed separately
            "中国台湾": "8",  # May need separate ID if Taiwan is listed separately
            "美国": "2",  # United States of America
            "日本": "17",  # Japan
            "韩国": "30",  # South Korea
            "英国": "12",  # United Kingdom
            "法国": "6",  # France
            "德国": "7",  # Germany
            "意大利": "9",  # Italy
            "西班牙": "23",  # Spain
            "印度": "70",  # India
            "澳大利亚": "20",  # Australia
        }
        
        for region in regions:
            if region in country_map:
                country_id = country_map[region]
                if country_id and country_id not in countries:
                    countries.append(country_id)
        
        return countries

    async def get_labels(self, meta: Meta) -> list[str]:
        """Get labels (中字, 中配, 4k) for MTEAM form"""
        labels = []
        
        # Check for Chinese subtitles
        if meta.get('is_disc', '') != 'BDMV':
            mi = cast(dict[str, Any], meta.get('mediainfo', {}))
            if mi:
                media = cast(dict[str, Any], mi.get('media', {}))
                tracks = cast(list[dict[str, Any]], media.get('track', []))
                for track in tracks:
                    if track['@type'] == "Text":
                        language = track.get('Language')
                        if language == "zh":
                            labels.append('中字')
                            break
        else:
            bdinfo = cast(dict[str, Any], meta.get('bdinfo', {}))
            if bdinfo:
                subtitles = cast(list[str], bdinfo.get('subtitles', []))
                for language in subtitles:
                    if language == "Chinese":
                        labels.append('中字')
                        break
        
        # Check for Chinese audio
        mi = cast(dict[str, Any], meta.get('mediainfo', {}))
        if mi:
            media = cast(dict[str, Any], mi.get('media', {}))
            tracks = cast(list[dict[str, Any]], media.get('track', []))
            for track in tracks:
                if track['@type'] == "Audio":
                    language = track.get('Language')
                    if language == "zh" or language == "chi":
                        labels.append('中配')
                        break
        
        # Add 4k label for 2160p/UHD content
        resolution = str(meta.get('resolution', '')).lower()
        if '2160p' in resolution or '4k' in resolution:
            labels.append('4k')
        
        return labels

    async def edit_desc(self, meta: Meta) -> None:
        async with aiofiles.open(f"{meta['base_dir']}/tmp/{meta['uuid']}/DESCRIPTION.txt", encoding='utf-8') as base_file:
            base = await base_file.read()

        from src.bbcode import BBCODE
        common = COMMON(config=self.config)

        parts: list[str] = []

        if int(meta.get('imdb_id', 0) or 0) != 0:
            ptgen = await common.ptgen(meta, self.ptgen_api, self.ptgen_retry)
            if ptgen.strip() != '':
                # Convert ptgen BBCode images to Markdown format for MTEAM
                ptgen_markdown = ptgen
                # Convert [img]...[/img] to ![](url)
                ptgen_markdown = re.sub(r'\[img\]([^\[]+?)\[/img\]', r'![](\1)', ptgen_markdown, flags=re.IGNORECASE)
                parts.append(ptgen_markdown)

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
        
        # Convert BBCode to Markdown format for MTEAM
        # Remove [center] and [/center] tags (Markdown doesn't need them)
        desc = desc.replace('[center]', '').replace('[/center]', '')
        
        # Convert BBCode images to Markdown format: [url=...][img]...[/img][/url] -> ![](url)
        # Match: [url=https://imgbox.com/xxx][img]https://...[/img][/url]
        desc = re.sub(r'\[url=([^\]]+)\]\[img\]([^\[]*?)\[/img\]\[/url\]', r'![](\1)', desc, flags=re.IGNORECASE | re.DOTALL)
        # Convert standalone [img]...[/img] -> ![](url)
        desc = re.sub(r'\[img\]([^\[]+?)\[/img\]', r'![](\1)', desc, flags=re.IGNORECASE)
        # Convert [img=size]...[/img] -> ![](url) (if any)
        desc = re.sub(r'\[img=\d+\]([^\[]+?)\[/img\]', r'![](\1)', desc, flags=re.IGNORECASE)
        
        parts.append(desc)

        images = cast(list[dict[str, Any]], meta.get('image_list', []))
        if len(images) > 0:
            # MTEAM uses Markdown format for images: ![](url)
            for each in range(len(images[:int(meta['screens'])])):
                img_url = images[each]['img_url']
                parts.append(f"![]({img_url})")

        if self.signature is not None:
            parts.append("\n\n")
            parts.append(self.signature)

        # Convert line endings to \r\n for MTEAM (Windows-style)
        final_desc = "".join(parts).replace('\n', '\r\n')
        async with aiofiles.open(f"{meta['base_dir']}/tmp/{meta['uuid']}/[{self.tracker}]DESCRIPTION.txt", 'w', encoding='utf-8') as descfile:
            await descfile.write(final_desc)

    async def edit_name(self, meta: Meta) -> str:
        mteam_name = str(meta.get('name', ''))

        remove_list = ['Dubbed', 'Dual-Audio']
        for each in remove_list:
            mteam_name = mteam_name.replace(each, '')

        mteam_name = mteam_name.replace(str(meta.get("aka", '')), '')
        mteam_name = mteam_name.replace('PQ10', 'HDR')

        if meta.get('type') == 'WEBDL' and meta.get('has_encode_settings', False) is True:
            mteam_name = mteam_name.replace('H.264', 'x264')

        return mteam_name

    async def get_mediainfo_text(self, meta: Meta) -> str:
        """Get MediaInfo text for MTEAM form
        For BDMV, use BD_FULL_00.txt (full BDInfo with all details)
        For other types, use MEDIAINFO.txt
        """
        if meta.get('bdinfo') is not None or meta.get('is_disc') == 'BDMV':
            # Use fixed full BDInfo file: BD_FULL_00.txt
            full_bdinfo_path = f"{meta['base_dir']}/tmp/{meta['uuid']}/BD_FULL_00.txt"
            
            if os.path.exists(full_bdinfo_path):
                mi_path = full_bdinfo_path
                console.print(f"[green]Using full BDInfo file: {os.path.basename(full_bdinfo_path)}[/green]")
            else:
                # Fallback to BD_SUMMARY_00.txt if FULL file doesn't exist
                mi_path = f"{meta['base_dir']}/tmp/{meta['uuid']}/BD_SUMMARY_00.txt"
                console.print(f"[yellow]BD_FULL_00.txt not found, falling back to BD_SUMMARY_00.txt[/yellow]")
        else:
            mi_path = f"{meta['base_dir']}/tmp/{meta['uuid']}/MEDIAINFO.txt"
        
        if os.path.exists(mi_path):
            async with aiofiles.open(mi_path, encoding='utf-8') as mi_file:
                content = await mi_file.read()
                # Convert line endings to \r\n for MTEAM
                return content.replace('\n', '\r\n')
        return ""

    async def upload(self, meta: Meta, _disctype: str) -> bool:
        """
        Upload torrent to MTEAM (mTorrent architecture).
        Uses multipart/form-data for file upload.
        """
        common = COMMON(config=self.config)
        await common.create_torrent_for_upload(meta, self.tracker, self.source_flag)

        desc_file = f"{meta['base_dir']}/tmp/{meta['uuid']}/[{self.tracker}]DESCRIPTION.txt"
        if not os.path.exists(desc_file):
            await self.edit_desc(meta)
        
        # Ensure ptgen is called and stored in meta if not already present
        # This is needed for smallDescr generation
        # ptgen is called in edit_desc(), but if desc file already exists, edit_desc() won't be called
        if not meta.get('ptgen') and int(meta.get('imdb_id', 0) or 0) != 0:
            console.print("[yellow]PTGEN not found in meta, calling ptgen API...[/yellow]")
            common = COMMON(config=self.config)
            ptgen_text = await common.ptgen(meta, self.ptgen_api, self.ptgen_retry)
            console.print(f"[yellow]PTGEN API returned text length: {len(ptgen_text) if ptgen_text else 0}[/yellow]")
            # ptgen() should have set meta['ptgen'], but let's verify
            if not meta.get('ptgen'):
                console.print("[red]Warning: ptgen() did not set meta['ptgen']![/red]")
                console.print(f"[red]imdb_id: {meta.get('imdb_id')}, ptgen_api: {self.ptgen_api}[/red]")

        mteam_name = await self.edit_name(meta)

        async with aiofiles.open(desc_file, encoding='utf-8') as desc_handle:
            mteam_desc = await desc_handle.read()
            # Ensure description is in Markdown format (convert if still BBCode)
            # Convert BBCode images to Markdown: [url=...][img]...[/img][/url] -> ![](url)
            mteam_desc = re.sub(r'\[url=([^\]]+)\]\[img\]([^\[]*?)\[/img\]\[/url\]', r'![](\1)', mteam_desc, flags=re.IGNORECASE | re.DOTALL)
            mteam_desc = re.sub(r'\[img\]([^\[]+?)\[/img\]', r'![](\1)', mteam_desc, flags=re.IGNORECASE)
            mteam_desc = re.sub(r'\[img=\d+\]([^\[]+?)\[/img\]', r'![](\1)', mteam_desc, flags=re.IGNORECASE)
            # Remove [center] tags
            mteam_desc = mteam_desc.replace('[center]', '').replace('[/center]', '')
        
        mediainfo_text = await self.get_mediainfo_text(meta)
        
        torrent_path = f"{meta['base_dir']}/tmp/{meta['uuid']}/[{self.tracker}].torrent"

        async with aiofiles.open(torrent_path, 'rb') as torrentFile:
            torrent_bytes = await torrentFile.read()
        filelist = cast(list[Any], meta.get('filelist', []))
        if len(filelist) == 1:
            torrentFileName = unidecode(os.path.basename(str(meta.get('video', ''))).replace(' ', '.'))
        else:
            torrentFileName = unidecode(os.path.basename(str(meta.get('path', ''))).replace(' ', '.'))
        
        # Prepare file for multipart/form-data upload (formData)
        # Field name must be 'file' according to actual website request
        files = {
            'file': (f"{torrentFileName}.torrent", torrent_bytes, "application/x-bittorent"),
        }

        # use chinese small_descr
        ptgen = cast(dict[str, Any], meta.get('ptgen', {}))
        
        # Debug: Print ptgen content
        console.print(f"[yellow]PTGEN Debug Info:[/yellow]")
        console.print(f"  ptgen exists: {bool(ptgen)}")
        if ptgen:
            console.print(f"  ptgen keys: {list(ptgen.keys())}")
            console.print(f"  chinese_title: {ptgen.get('chinese_title', 'NOT FOUND')}")
            console.print(f"  trans_title: {ptgen.get('trans_title', 'NOT FOUND')}")
            console.print(f"  genre: {ptgen.get('genre', 'NOT FOUND')}")
        
        # ptgen API returns 'chinese_title' (string) instead of 'trans_title' (list)
        # Also check for 'trans_title' for backward compatibility
        chinese_title = ""
        if ptgen:
            chinese_title = ptgen.get('chinese_title', '') or ptgen.get('trans_title', '')
            if isinstance(chinese_title, list) and len(chinese_title) > 0:
                chinese_title = chinese_title[0]
            chinese_title = str(chinese_title).strip() if chinese_title else ""
        
        genres = cast(list[str], ptgen.get("genre", [])) if ptgen else []
        console.print(f"  Extracted chinese_title: {chinese_title}")
        console.print(f"  Filtered genres: {genres}")
        
        # Try to extract subtitle info from description
        subtitle_info = ""
        desc_content = mteam_desc
        # Look for subtitle patterns in description (e.g., "DiY官译简繁英+简英繁英双语字幕")
        # Check in the original DESCRIPTION.txt file as well
        try:
            desc_file_path = f"{meta['base_dir']}/tmp/{meta['uuid']}/DESCRIPTION.txt"
            if os.path.exists(desc_file_path):
                async with aiofiles.open(desc_file_path, encoding='utf-8') as orig_desc_file:
                    orig_desc = await orig_desc_file.read()
                    desc_content = orig_desc + "\n" + desc_content
        except Exception:
            pass
        
        # Look for subtitle patterns - check for common subtitle descriptions
        subtitle_patterns = [
            r'DiY[^|\n]*字幕[^|\n]*',
            r'官译[^|\n]*',
            r'[^|\n]*字幕[^|\n]*',
            r'双语字幕',
        ]
        for pattern in subtitle_patterns:
            match = re.search(pattern, desc_content)
            if match:
                subtitle_info = match.group(0).strip()
                # Clean up common prefixes/suffixes
                subtitle_info = re.sub(r'^[^:]*[:：]\s*', '', subtitle_info)
                break
        
        # Build smallDescr
        if chinese_title:
            # Use Chinese title from ptgen
            small_descr = chinese_title
            # Add subtitle info if available
            if subtitle_info:
                small_descr += f" | {subtitle_info}"
            else:
                # Fallback to genre if no subtitle info
                genre_value = genres[0] if genres and len(genres) > 0 and genres[0].strip() else ''
                if genre_value:
                    small_descr += f" | 类别:{genre_value}"
        else:
            # Fallback to English title if no Chinese title available
            small_descr = str(meta.get('title', 'Monsters of Man'))
            if subtitle_info:
                small_descr += f" | {subtitle_info}"
        
        # Build form data according to MTEAM form structure
        data: dict[str, Any] = {
            "name": mteam_name,
            "smallDescr": small_descr,
            "descr": mteam_desc,
        }
        
        # Add category (required, integer)
        category_id = await self.get_category_id(meta)
        if category_id:
            data["category"] = category_id
        else:
            console.print("[red]Failed to determine category ID[/red]")
            return False
        
        # Add resolution (optional, integer)
        standard_id = await self.get_standard_id(meta)
        if standard_id is not None:
            data["standard"] = standard_id
        
        # Add video codec (optional, integer)
        video_codec = await self.get_video_codec_id(meta)
        if video_codec is not None:
            data["videoCodec"] = video_codec
        
        # Add audio codec (optional, integer)
        audio_codec = await self.get_audio_codec_id(meta)
        if audio_codec is not None:
            data["audioCodec"] = audio_codec
        
        # Add countries (optional, array format)
        countries = await self.get_countries(meta)
        if countries:
            data["countries"] = countries  # Array format, not comma-separated string
        
        # Add labels (optional, array format)
        labels = await self.get_labels(meta)
        if labels:
            data["labelsNew"] = labels  # Array format, not comma-separated string
        
        # Add MediaInfo
        if mediainfo_text:
            data["mediainfo"] = mediainfo_text
        
        # Add IMDb URL if available
        imdb_id = int(meta.get('imdb_id', 0) or 0)
        if imdb_id != 0:
            data["imdb"] = f"https://www.imdb.com/title/tt{meta.get('imdb', '')}"  # No trailing slash
        
        # Add Douban URL if available
        douban_url = ""
        if ptgen:
            douban_value = ptgen.get("douban", "")
            if douban_value:
                if isinstance(douban_value, str) and douban_value.startswith("http"):
                    douban_url = douban_value
                elif isinstance(douban_value, str) and douban_value.isdigit():
                    douban_url = f"https://movie.douban.com/subject/{douban_value}/"
        if douban_url:
            data["douban"] = douban_url
        
        # Add anonymous upload (required, boolean)
        data["anonymous"] = bool(meta.get('anon') == 1 or self.config['TRACKERS'][self.tracker].get('anon', False))
        
        # Add optional empty fields that may be expected by the API
        data["dmmCode"] = ""
        data["tags"] = ""
        data["aids"] = ""
        data["mediainfoAnalysisResult"] = None

        url = "https://api.m-team.cc/api/torrent/createOredit"

        # Submit using API key authentication
        if meta.get('debug'):
            console.print(url)
            console.print(data)
            meta['tracker_status'][self.tracker]['status_message'] = "Debug mode enabled, not uploading."
            return True  # Debug mode - simulated success
        else:
            if not self.api_key:
                console.print("[bold red]Missing API key. Please set api_key in config.")
                return False
            
            try:
                # Ensure x-api-key header is set for this request
                headers = {
                    'x-api-key': self.api_key,
                    'accept': 'application/json',
                }
                
                # Use multipart/form-data for file upload (formData)
                # httpx automatically uses multipart/form-data when files parameter is provided
                # The 'file' field contains the binary torrent file
                up = await self.session.post(url=url, data=data, files=files, headers=headers)
                    
                # Check if upload was successful
                if up.status_code == 200:
                    # Parse JSON response according to API docs
                    try:
                        response_json = up.json()
                        # API response format: {"code": 0 or "0", "message": "", "data": {}}
                        # Handle both string and integer code values
                        code = response_json.get('code')
                        if code == 0 or code == "0" or str(code) == "0":
                            console.print(f"[green]Uploaded to MTEAM successfully[/green]")
                            meta['tracker_status'][self.tracker]['status_message'] = "Upload successful"
                            
                            # Try to extract torrent ID from response data
                            data_obj = response_json.get('data', {})
                            if isinstance(data_obj, dict):
                                torrent_id = data_obj.get('id') or data_obj.get('torrentId')
                                if torrent_id:
                                    meta['tracker_status'][self.tracker]['torrent_id'] = str(torrent_id)
                            
                            return True
                        else:
                            error_msg = response_json.get('message', 'Unknown error')
                            console.print(f"[red]Upload failed: {error_msg}[/red]")
                            meta['tracker_status'][self.tracker]['status_message'] = f"Upload failed: {error_msg}"
                            raise UploadException(f"Upload to MTEAM Failed: {error_msg}", 'red')  # noqa #F405
                    except Exception as json_error:
                        console.print(f"[yellow]Failed to parse response JSON: {json_error}[/yellow]")
                        console.print(f"[yellow]Response text: {up.text[:500]}[/yellow]")
                        raise UploadException(f"Upload to MTEAM Failed: Invalid response format", 'red')  # noqa #F405
                
                # If we get here, upload failed
                console.print(data)
                console.print("\n\n")
                console.print(f"[yellow]Response URL: {up.url}[/yellow]")
                console.print(f"[yellow]Response status: {up.status_code}[/yellow]")
                
                # Provide more detailed error information for 403 errors
                if up.status_code == 403:
                    error_msg = "Authentication failed (403 Forbidden). Please check your API key."
                    if up.headers.get('content-type', '').startswith('application/json'):
                        try:
                            error_json = up.json()
                            error_detail = error_json.get('error') or error_json.get('message') or str(error_json)
                            error_msg += f" Server response: {error_detail}"
                        except Exception:
                            pass
                    console.print(f"[red]{error_msg}[/red]")
                    raise UploadException(f"Upload to MTEAM Failed: {error_msg}", 'red')  # noqa #F405
                
                if up.headers.get('content-type', '').startswith('application/json'):
                    try:
                        console.print(f"[yellow]Response JSON: {up.json()}[/yellow]")
                    except Exception:
                        pass
                raise UploadException(f"Upload to MTEAM Failed: result URL {up.url} ({up.status_code}) was not expected", 'red')  # noqa #F405
            except httpx.RequestError as e:
                console.print(f"[red]Request error: {e}[/red]")
                raise UploadException(f"Upload to MTEAM Failed: {e}", 'red')  # noqa #F405
        return False
