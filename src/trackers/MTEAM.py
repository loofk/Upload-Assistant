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
        self.rehost_images = bool(config['TRACKERS']['MTEAM'].get('img_rehost', False))
        self.ptgen_api = str(config['TRACKERS']['MTEAM'].get('ptgen_api', '')).strip()

        self.ptgen_retry = 3
        self.signature: Optional[str] = None
        self.banned_groups: list[str] = [""]
        
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
            # Test API key by making a request to user profile or similar endpoint
            url = "https://kp.m-team.cc/api/user/profile"
            response = await self.session.get(url)
            if response.status_code == 200:
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
        search_url = f"https://kp.m-team.cc/api/torrents"
        
        try:
            params = {'imdb': imdb}
            response = await self.session.get(search_url, params=params)
            
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

    async def get_category_id(self, meta: Meta) -> Optional[str]:
        """Get category ID for MTEAM form based on actual site categories"""
        category = str(meta.get('category', ''))
        resolution = str(meta.get('resolution', '')).lower()
        media_type = str(meta.get('type', ''))
        is_disc = meta.get('is_disc', '')
        
        # 電影 (Movie) categories
        if category == 'MOVIE':
            # 電影/Blu-Ray
            if is_disc == 'BDMV':
                return '421'
            # 電影/DVDiSo
            elif is_disc == 'DVD':
                return '420'
            # 電影/Remux
            elif media_type == 'REMUX':
                return '439'
            # 電影/HD (1080p, 720p, 4K)
            elif resolution in ('1080p', '720p', '2160p', '4k'):
                return '419'
            # 電影/SD (default for movies)
            else:
                return '401'
        
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
                    return '438'
                # 影劇/綜藝/DVDiSo
                elif is_disc == 'DVD':
                    return '435'
                # 影劇/綜藝/HD
                elif resolution in ('1080p', '720p', '2160p', '4k'):
                    return '402'
                # 影劇/綜藝/SD
                else:
                    return '403'
            else:
                # For regular TV series, use HD category
                if is_disc == 'BDMV':
                    return '438'  # 影劇/綜藝/BD
                elif is_disc == 'DVD':
                    return '435'  # 影劇/綜藝/DVDiSo
                elif resolution in ('1080p', '720p', '2160p', '4k'):
                    return '402'  # 影劇/綜藝/HD
                else:
                    return '403'  # 影劇/綜藝/SD
        
        # 紀錄 (Documentary)
        genres_value = meta.get("genres", "")
        genres = ', '.join(cast(list[str], genres_value)) if isinstance(genres_value, list) else str(genres_value)
        keywords_value = meta.get("keywords", "")
        keywords = ', '.join(cast(list[str], keywords_value)) if isinstance(keywords_value, list) else str(keywords_value)
        
        if 'documentary' in genres.lower() or 'documentary' in keywords.lower():
            return '404'  # 紀錄
        
        # 動畫 (Animation)
        if 'animation' in genres.lower() or 'animation' in keywords.lower() or meta.get('anime', False):
            return '405'  # 動畫
        
        # 運動 (Sports)
        if 'sport' in genres.lower() or 'sports' in genres.lower():
            return '407'  # 運動
        
        # Default to Misc
        return '409'  # Misc(其他)

    async def get_standard_id(self, meta: Meta) -> Optional[str]:
        """Get resolution/standard ID for MTEAM form"""
        resolution = str(meta.get('resolution', '')).lower()
        
        if '8k' in resolution or '7680' in resolution:
            return '8K'
        elif '4k' in resolution or '2160p' in resolution or '2160i' in resolution:
            return '4K'
        elif '1080p' in resolution:
            return '1080p'
        elif '1080i' in resolution:
            return '1080i'
        elif '720p' in resolution or '720i' in resolution:
            return '720p'
        elif '480p' in resolution or '480i' in resolution or '576p' in resolution or '576i' in resolution:
            return 'SD'
        else:
            return None

    async def get_video_codec_id(self, meta: Meta) -> Optional[str]:
        """Get video codec ID for MTEAM form"""
        mi = cast(dict[str, Any], meta.get('mediainfo', {}))
        video_tracks = []
        if mi:
            media = cast(dict[str, Any], mi.get('media', {}))
            tracks = cast(list[dict[str, Any]], media.get('track', []))
            video_tracks = [t for t in tracks if t.get('@type') == 'Video']
        
        if video_tracks:
            codec = str(video_tracks[0].get('Format', '')).upper()
            if 'HEVC' in codec or 'H.265' in codec or 'X265' in codec:
                return 'H.265'
            elif 'AVC' in codec or 'H.264' in codec or 'X264' in codec:
                return 'H.264'
            elif 'VC-1' in codec:
                return 'VC-1'
            elif 'MPEG-2' in codec or 'MPEG2' in codec:
                return 'MPEG-2'
            elif 'AV1' in codec:
                return 'AV1'
        
        return None

    async def get_audio_codec_id(self, meta: Meta) -> Optional[str]:
        """Get audio codec ID for MTEAM form"""
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
                return 'DTS:X'
            elif 'ATMOS' in format_profile or 'TRUEHD ATMOS' in codec:
                return 'TrueHD Atmos'
            elif 'DTS-HD' in codec or 'DTSHD' in codec:
                return 'DTS-HD MA'
            elif 'TRUEHD' in codec:
                return 'TrueHD'
            elif 'LPCM' in codec or 'PCM' in codec:
                return 'LPCM'
            elif 'DTS' in codec:
                return 'DTS'
            elif 'AC3' in codec or 'DD' in codec or 'DOLBY DIGITAL' in codec:
                return 'AC3'
            elif 'OPUS' in codec:
                return 'OPUS'
            elif 'AAC' in codec:
                return 'AAC'
            elif 'FLAC' in codec:
                return 'FLAC'
        
        return None

    async def get_countries(self, meta: Meta) -> list[str]:
        """Get country/region IDs for MTEAM form (multi-select)"""
        countries = []
        ptgen = cast(dict[str, Any], meta.get('ptgen', {}))
        regions_value = ptgen.get("region", [])
        regions = cast(list[str], regions_value) if isinstance(regions_value, list) else []
        
        # Map regions to country codes/names - adjust based on actual site options
        country_map = {
            "中国大陆": "CN",
            "中国香港": "HK",
            "中国台湾": "TW",
            "美国": "US",
            "日本": "JP",
            "韩国": "KR",
            "英国": "GB",
            "法国": "FR",
            "德国": "DE",
            "意大利": "IT",
            "西班牙": "ES",
            "印度": "IN",
        }
        
        for region in regions:
            if region in country_map:
                country_code = country_map[region]
                if country_code not in countries:
                    countries.append(country_code)
        
        return countries

    async def get_labels(self, meta: Meta) -> list[str]:
        """Get labels (中字, 中配) for MTEAM form"""
        labels = []
        
        # Check for Chinese subtitles
        if meta.get('is_disc', '') != 'BDMV':
            mi = cast(dict[str, Any], meta.get('mediainfo', {}))
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
        """Get MediaInfo text for MTEAM form"""
        if meta.get('bdinfo') is not None:
            mi_path = f"{meta['base_dir']}/tmp/{meta['uuid']}/BD_SUMMARY_00.txt"
        else:
            mi_path = f"{meta['base_dir']}/tmp/{meta['uuid']}/MEDIAINFO.txt"
        
        if os.path.exists(mi_path):
            async with aiofiles.open(mi_path, encoding='utf-8') as mi_file:
                return await mi_file.read()
        return ""

    async def upload(self, meta: Meta, _disctype: str) -> bool:
        """
        Upload torrent to MTEAM (mTorrent architecture).
        Uses multipart/form-data or JSON API based on site implementation.
        """
        common = COMMON(config=self.config)
        await common.create_torrent_for_upload(meta, self.tracker, self.source_flag)

        desc_file = f"{meta['base_dir']}/tmp/{meta['uuid']}/[{self.tracker}]DESCRIPTION.txt"
        if not os.path.exists(desc_file):
            await self.edit_desc(meta)

        mteam_name = await self.edit_name(meta)

        async with aiofiles.open(desc_file, encoding='utf-8') as desc_handle:
            mteam_desc = await desc_handle.read()
        
        mediainfo_text = await self.get_mediainfo_text(meta)
        
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
        
        # Build form data according to MTEAM form structure
        data: dict[str, Any] = {
            "name": mteam_name,
            "smallDescr": small_descr,
            "descr": mteam_desc,
        }
        
        # Add category
        category_id = await self.get_category_id(meta)
        if category_id:
            data["category"] = category_id
        
        # Add resolution
        standard_id = await self.get_standard_id(meta)
        if standard_id:
            data["standard"] = standard_id
        
        # Add video codec
        video_codec = await self.get_video_codec_id(meta)
        if video_codec:
            data["videoCodec"] = video_codec
        
        # Add audio codec
        audio_codec = await self.get_audio_codec_id(meta)
        if audio_codec:
            data["audioCodec"] = audio_codec
        
        # Add countries (multi-select)
        countries = await self.get_countries(meta)
        if countries:
            data["countries"] = countries
        
        # Add labels (checkboxes)
        labels = await self.get_labels(meta)
        if labels:
            data["labelsNew"] = labels
        
        # Add MediaInfo
        if mediainfo_text:
            data["mediainfo"] = mediainfo_text
        
        # Add IMDb URL if available
        imdb_id = int(meta.get('imdb_id', 0) or 0)
        if imdb_id != 0:
            data["imdb"] = f"https://www.imdb.com/title/tt{meta.get('imdb', '')}/"
        
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
        
        # Add anonymous upload
        if meta.get('anon') == 1 or self.config['TRACKERS'][self.tracker].get('anon', False):
            data["anonymous"] = True

        url = "https://kp.m-team.cc/api/torrents/upload"

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
                # mTorrent architecture typically uses JSON API with multipart for files
                # Try multipart/form-data first
                try:
                    up = await self.session.post(url=url, data=data, files=files)
                    
                    # Check if upload was successful
                    if up.status_code == 200 or up.status_code == 201:
                        # Try to parse JSON response
                        try:
                            response_json = up.json()
                            if response_json.get('success') or response_json.get('status') == 'success' or 'id' in response_json:
                                torrent_id = str(response_json.get('id', ''))
                                console.print(f"[green]Uploaded to MTEAM successfully[/green]")
                                meta['tracker_status'][self.tracker]['status_message'] = "Upload successful"
                                if torrent_id:
                                    meta['tracker_status'][self.tracker]['torrent_id'] = torrent_id
                                return True
                        except Exception:
                            # If not JSON, check URL redirect
                            if 'torrent' in str(up.url).lower() or 'details' in str(up.url).lower():
                                console.print(f"[green]Uploaded to MTEAM: [yellow]{str(up.url)}[/yellow][/green]")
                                id_match = re.search(r"(torrents?/|id=)(\d+)", str(up.url))
                                if id_match is not None:
                                    torrent_id = id_match.group(2)
                                    meta['tracker_status'][self.tracker]['status_message'] = str(up.url)
                                    meta['tracker_status'][self.tracker]['torrent_id'] = torrent_id
                                else:
                                    meta['tracker_status'][self.tracker]['status_message'] = "Upload successful"
                                return True
                            elif 'success' in up.text.lower() or '成功' in up.text:
                                console.print(f"[green]Uploaded to MTEAM successfully[/green]")
                                meta['tracker_status'][self.tracker]['status_message'] = "Upload successful"
                                return True
                except Exception as multipart_error:
                    console.print(f"[yellow]Multipart upload failed, trying JSON API: {multipart_error}[/yellow]")
                    # Try JSON API with base64 encoded file
                    try:
                        import base64
                        torrent_b64 = base64.b64encode(torrent_bytes).decode('utf-8')
                        json_data = data.copy()
                        json_data['file'] = torrent_b64
                        json_data['filename'] = f"{torrentFileName}.torrent"
                        
                        up = await self.session.post(url=url, json=json_data)
                        
                        if up.status_code == 200 or up.status_code == 201:
                            response_json = up.json() if up.headers.get('content-type', '').startswith('application/json') else {}
                            if response_json.get('success') or response_json.get('status') == 'success' or 'id' in response_json:
                                console.print(f"[green]Uploaded to MTEAM via JSON API[/green]")
                                meta['tracker_status'][self.tracker]['status_message'] = "Upload successful"
                                torrent_id = str(response_json.get('id', ''))
                                if torrent_id:
                                    meta['tracker_status'][self.tracker]['torrent_id'] = torrent_id
                                return True
                    except Exception as json_error:
                        console.print(f"[red]JSON API upload also failed: {json_error}[/red]")
                
                # If we get here, upload failed
                console.print(data)
                console.print("\n\n")
                console.print(f"[yellow]Response URL: {up.url}[/yellow]")
                console.print(f"[yellow]Response status: {up.status_code}[/yellow]")
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
