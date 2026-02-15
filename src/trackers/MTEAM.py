import os
import re
from typing import Any, Optional, Union, cast

import aiofiles
import httpx
from unidecode import unidecode

from src.console import console
from src.exceptions import UploadException  # noqa E403
from src.trackers.COMMON import COMMON

Meta = dict[str, Any]
Config = dict[str, Any]

# standardList.json: 1=1080p, 2=1080i, 3=720p, 5=SD, 6=4K, 7=8K（与 get_standard_id 一致）
STANDARD_ID_TO_RES: dict[str, str] = {
    "1": "1080p", "2": "1080i", "3": "720p", "5": "SD", "6": "2160p", "7": "8K",
}
# sourceList.json: 8=Web-DL, 1=Bluray, 4=Remux, 5=HDTV/TV, 3=DVD, 6=Other
SOURCE_ID_TO_TYPE: dict[str, str] = {
    "8": "WEBDL", "1": "BluRay", "4": "REMUX", "5": "HDTV", "3": "DVD", "6": "Other",
}


def _standard_id_to_res(standard_id: Any) -> str:
    """API 返回的 standard ID -> 分辨率名称，供 search_existing 填 DupeEntry.res"""
    return STANDARD_ID_TO_RES.get(str(standard_id).strip(), "")


def _parse_codec_ids_from_mediainfo_text(text: str) -> tuple[Optional[int], Optional[int]]:
    """Parse video/audio codec IDs from BDInfo or MediaInfo raw text when structured meta['mediainfo'] is missing (e.g. BDMV).
    Returns (video_codec_id or None, audio_codec_id or None). MTEAM IDs: video 1=H.264, 16=H.265, 2=VC-1, 4=MPEG-2, 19=AV1, 21=VP8/9; audio 8=AC3, 3=DTS, 11=DTS-HD MA, 12=E-AC3, 9=TrueHD, 10=TrueHD Atmos, 6=AAC, 1=FLAC, 14=LPCM.
    """
    if not text or not isinstance(text, str):
        return None, None
    t = text.upper()
    video_id: Optional[int] = None
    audio_id: Optional[int] = None
    # Video: order matters (more specific first)
    if 'HEVC' in t or 'H.265' in t or 'X265' in t or 'H265' in t:
        video_id = 16
    elif 'AVC' in t or 'H.264' in t or 'X264' in t or 'H264' in t or 'MPEG-4 AVC' in t or 'MPEG4 AVC' in t:
        video_id = 1
    elif 'VC-1' in t or 'VC1' in t:
        video_id = 2
    elif 'MPEG-2' in t or 'MPEG2' in t:
        video_id = 4
    elif 'AV1' in t:
        video_id = 19
    elif 'VP9' in t or 'VP8' in t:
        video_id = 21
    elif 'XVID' in t:
        video_id = 3
    # Audio: order matters (more specific first)
    if 'E-AC3' in t and ('ATMOS' in t or 'ATOMS' in t):
        audio_id = 13
    elif 'E-AC3' in t or 'DDP' in t or 'DOLBY DIGITAL PLUS' in t:
        audio_id = 12
    elif 'TRUEHD' in t and 'ATMOS' in t:
        audio_id = 10
    elif 'TRUEHD' in t or 'TRUE HD' in t:
        audio_id = 9
    elif 'DTS-HD' in t or 'DTSHD' in t or 'DTS-HD MA' in t:
        audio_id = 11
    elif 'DTS:X' in t or 'DTSX' in t:
        audio_id = 3
    elif 'DTS' in t:
        audio_id = 3
    elif 'LPCM' in t or 'PCM' in t:
        audio_id = 14
    elif 'FLAC' in t:
        audio_id = 1
    elif 'AC3' in t or 'DOLBY DIGITAL' in t or 'DD ' in t:
        audio_id = 8
    elif 'AAC' in t:
        audio_id = 6
    elif 'MP3' in t or 'MP2' in t:
        audio_id = 4
    elif 'OGG' in t or 'VORBIS' in t:
        audio_id = 5
    return video_id, audio_id


def _source_id_to_type(source_id: Any) -> str:
    """API 返回的 source ID -> 类型名称，供 search_existing 填 DupeEntry.type"""
    return SOURCE_ID_TO_TYPE.get(str(source_id).strip(), "")


def _infer_type_from_name(name: str) -> str:
    """从种子名称推断来源类型（API 无 source 时回退）"""
    n = name.lower()
    if "web-dl" in n or "webdl" in n or "web dl" in n or "amzn" in n or "nf " in n or "atvp" in n:
        return "WEBDL"
    if "blu-ray" in n or "bluray" in n or "uhd blu" in n or "bdrip" in n:
        return "BluRay"
    if "hdtv" in n or "pdtv" in n:
        return "HDTV"
    if "remux" in n:
        return "REMUX"
    return ""


def _infer_res_from_name(name: str) -> str:
    """从种子名称推断分辨率（API 无 standard 时回退）"""
    n = name.lower()
    if "2160p" in n or "4k" in n or "uhd" in n:
        return "2160p"
    if "1080p" in n or "1080i" in n:
        return "1080p"
    if "720p" in n or "720i" in n:
        return "720p"
    if "480p" in n or "576p" in n:
        return "SD"
    return ""


class MTEAMRequestError(Exception):
    """MTEAM 请求失败，由 _request 在失败时抛出。status 为 0 表示网络异常，否则为 HTTP 状态码。"""
    def __init__(self, message: str, status: int = 0) -> None:
        self.message = message
        self.status = status
        super().__init__(message)


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

    @staticmethod
    def _parse_api_response(response_json: dict) -> tuple[bool, Any, str]:
        """解析 MTEAM 统一响应格式 {"code": 0 or "0", "message": "", "data": {}}。
        返回 (success, data, message)。
        """
        code = response_json.get('code')
        success = code == 0 or code == "0" or str(code) == "0"
        data = response_json.get('data', {})
        message = response_json.get('message', '')
        return success, data, message

    async def _request(
        self,
        url: str,
        *,
        data: Optional[dict] = None,
        json: Optional[dict] = None,
        files: Optional[dict] = None,
    ) -> Any:
        """通用请求：POST 到 url，内部处理 status 与 code。
        data 为 form 表单，json 为 JSON 体，二者互斥。
        成功返回 data；失败则打印错误并抛出 MTEAMRequestError(message, status)。
        """
        try:
            if json is not None:
                response = await self.session.post(url, json=json)
            else:
                response = await self.session.post(url, data=data, files=files)
        except httpx.TimeoutException as e:
            msg = f"Request timed out: {e}"
            console.print(f"[red]{msg}[/red]")
            raise MTEAMRequestError(msg, 0) from e
        except httpx.RequestError as e:
            msg = str(e)
            console.print(f"[red]{msg}[/red]")
            raise MTEAMRequestError(msg, 0) from e
        status = response.status_code
        if status != 200:
            msg = response.text[:200] if response.text else f"HTTP {status}"
            if response.headers.get('content-type', '').startswith('application/json'):
                try:
                    body = response.json()
                    if isinstance(body, dict):
                        msg = body.get('message') or body.get('error') or msg
                except Exception:
                    pass
            if status == 403 or status == 401:
                msg = "Authentication failed (403 Forbidden or 401 Unauthorized). Please check your API key." + (f" {msg}" if msg else "")
            console.print(f"[red]{msg}[/red]")
            raise MTEAMRequestError(msg, status)
        try:
            body = response.json()
        except Exception:
            msg = "Invalid JSON"
            console.print(f"[red]{msg}[/red]")
            raise MTEAMRequestError(msg, 200)
        if not isinstance(body, dict):
            msg = "Response is not dict"
            console.print(f"[red]{msg}[/red]")
            raise MTEAMRequestError(msg, 200)
        success, data, message = self._parse_api_response(body)
        if not success:
            msg = message or "API returned error"
            console.print(f"[red]{msg}[/red]")
            raise MTEAMRequestError(msg, 200)
        return data

    async def validate_credentials(self, meta: Meta) -> bool:
        """Validate API key by making a test request"""
        if not self.api_key:
            console.print('[red]Failed to validate API key. Please set api_key in config.')
            return False
        
        url = "https://api.m-team.cc/api/member/profile"
        try:
            await self._request(url, data={'uid': self.uid})
            return True
        except MTEAMRequestError as e:
            return False

    async def search_existing(self, meta: Meta, _disctype: str) -> Union[list[str], list[dict[str, Any]], bool]:
        """Search for existing torrents using API.
        返回 list[dict] 以兼容 dupe_checking：每条包含 name，以及 type/res/id/link/size 等（API 有则填，无则从 name 推断）。
        这样 filter_dupes 的「source mismatch」等规则能正确排除 WEB-DL vs 蓝光 等，且 debug 时展示的重复项不会全是空字段。
        """
        dupes: list[dict[str, Any]] = []
        imdb_id = int(meta.get('imdb_id', 0) or 0)
        if imdb_id == 0:
            return dupes
        
        imdb = f"tt{meta.get('imdb', '')}"
        search_url = "https://api.m-team.cc/api/torrent/search"
        payload = {
            "mode": "normal",
            "visible": 1,
            "categories": [],
            "pageNumber": 1,
            "pageSize": 100,
            "imdb": imdb,
        }
        try:
            data = await self._request(search_url, json=payload)
        except MTEAMRequestError:
            return dupes
        torrents = data if isinstance(data, list) else (data.get('data', []) or data.get('torrents', []) if isinstance(data, dict) else [])
        if not isinstance(torrents, list):
            torrents = []
        console.print(f"[green]获取到 {len(torrents)} 个种子[/green]")
        for torrent in torrents:
            if not isinstance(torrent, dict):
                continue
            name = torrent.get('name') or torrent.get('title', '')
            if not name:
                continue
            name = str(name)
            # 实际 API 返回：id, name, smallDescr, standard, source, numfiles, size, status, ...
            tid = torrent.get("id")
            numfiles = torrent.get("numfiles")
            file_count = int(numfiles) if numfiles is not None and str(numfiles).isdigit() else 0
            standard_id = torrent.get("standard")
            source_id = torrent.get("source")
            # standardList: 1=1080p, 2=1080i, 3=720p, 5=SD, 6=4K, 7=8K
            res = _standard_id_to_res(standard_id) if standard_id is not None else _infer_res_from_name(name)
            # sourceList: 8=Web-DL, 1=Bluray, 4=Remux, 5=HDTV/TV, 3=DVD, 6=Other
            type_str = _source_id_to_type(source_id) if source_id is not None else _infer_type_from_name(name)
            link = f"https://kp.m-team.cc/details/{tid}" if tid else None
            entry = {
                "name": name,
                "size": torrent.get("size"),
                "files": [],
                "file_count": file_count,
                "trumpable": False,
                "link": link,
                "download": None,
                "flags": list(torrent.get("labelsNew", [])) if isinstance(torrent.get("labelsNew"), list) else [],
                "id": tid,
                "type": type_str,
                "res": res,
                "internal": 0,
                "bd_info": None,
                "description": torrent.get("smallDescr"),
            }
            dupes.append(entry)
        return dupes

    async def download_new_torrent(self, torrent_id: str, torrent_path: str) -> None:
        """
        Download the torrent file using the new credential mechanism.
        Steps:
        1. Call /api/torrent/genDlToken with formData parameter ID (torrent ID)
        2. Get the data value from response (this is the actual torrent download URL)
        3. Download the torrent file using GET request
        """
        if not self.api_key:
            console.print("[red]MTEAM API key not configured, cannot download torrent[/red]")
            return

        try:
            gen_token_url = "https://api.m-team.cc/api/torrent/genDlToken"
            token_data = {"id": torrent_id}
            download_url = await self._request(gen_token_url, data=token_data)

            if not download_url:
                console.print("[red]No download URL found in genDlToken response[/red]")
                return

            if not isinstance(download_url, str):
                console.print(f"[red]Unexpected response format from genDlToken: expected string, got {type(download_url)}[/red]")
                return

            console.print(f"[cyan]Downloading MTEAM torrent from: {download_url}[/cyan]")
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(download_url)
                response.raise_for_status()
                async with aiofiles.open(torrent_path, "wb") as torrent_file:
                    await torrent_file.write(response.content)
            console.print(f"[green]Successfully downloaded MTEAM torrent to: {torrent_path}[/green]")

        except MTEAMRequestError as e:
            console.print(f"[red]Failed to generate download token: {e.message}[/red]")
        except httpx.RequestError as e:
            console.print(f"[red]Failed to download torrent file: {e}[/red]")
        except Exception as e:
            console.print(f"[red]Unexpected error downloading MTEAM torrent: {e}[/red]")
            if self.config.get('DEFAULT', {}).get('debug', False):
                console.print_exception()

    async def get_info_from_torrent_id(self, mteam_id: Union[int, str], meta: Optional[Meta] = None) -> tuple[Optional[int], Optional[int], Optional[str], Optional[str], Optional[str]]:
        """
        Fetch metadata from MTEAM torrent details using API.
        Returns: (imdb_id, tmdb_id, name, torrenthash, description)
        """
        mteam_imdb = mteam_tmdb = mteam_name = mteam_torrenthash = mteam_description = None
        
        if not self.api_key:
            console.print("[bold red]MTEAM API key not configured[/bold red]")
            return mteam_imdb, mteam_tmdb, mteam_name, mteam_torrenthash, mteam_description
        
        url = "https://api.m-team.cc/api/torrent/detail"
        payload = {"id": int(mteam_id)}
        
        try:
            data = await self._request(url, json=payload)
            
            if isinstance(data, dict):
                # Extract IMDb ID from imdb link
                imdb_link = data.get('imdb', '')
                if imdb_link and isinstance(imdb_link, str):
                    imdb_match = re.search(r'tt(\d+)', imdb_link)
                    if imdb_match:
                        mteam_imdb = int(imdb_match.group(1))
                
                # Extract Douban ID and URL from douban link
                douban_link = data.get('douban', '')
                if douban_link and isinstance(douban_link, str) and meta:
                    # Normalize URL (ensure it's a complete URL)
                    if not douban_link.startswith('http'):
                        douban_link = f"https://movie.douban.com/subject/{douban_link}"
                    douban_match = re.search(r'/subject/(\d+)', douban_link)
                    if douban_match:
                        douban_id = douban_match.group(1)
                        douban_url = f"https://movie.douban.com/subject/{douban_id}/"
                        meta['douban_id'] = meta['douban'] = douban_id
                        meta['douban_url'] = douban_url
                        console.print(f"[green]MTEAM: Found Douban ID: {douban_id}, URL: {douban_url}[/green]")
                
                # Extract TMDb ID (if available in API response)
                tmdb_link = data.get('tmdb', '')
                if tmdb_link and isinstance(tmdb_link, str):
                    tmdb_match = re.search(r'/(movie|tv)/(\d+)', tmdb_link)
                    if tmdb_match:
                        mteam_tmdb = int(tmdb_match.group(2))
                
                # Extract torrent name
                mteam_name = data.get('name') or data.get('title', '')
                
                # Extract description
                mteam_description = data.get('descr') or data.get('description', '')
                
                # Extract torrent hash (if available)
                mteam_torrenthash = data.get('hash') or data.get('infoHash', '')
                
        except MTEAMRequestError as e:
            console.print(f"[red]MTEAM API request failed: {e.message}[/red]")
        except Exception as e:
            console.print(f"[red]Unexpected error fetching MTEAM details: {e}[/red]")
            if meta and meta.get('debug', False):
                console.print_exception()
            elif self.config.get('DEFAULT', {}).get('debug', False):
                console.print_exception()
        
        return mteam_imdb, mteam_tmdb, mteam_name, mteam_torrenthash, mteam_description

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
            genres_value = meta.get("genres", "")
            genres = ', '.join(cast(list[str], genres_value)) if isinstance(genres_value, list) else str(genres_value)
            keywords_value = meta.get("keywords", "")
            keywords = ', '.join(cast(list[str], keywords_value)) if isinstance(keywords_value, list) else str(keywords_value)
            # 動畫 (Animation) must be checked before 綜藝 so anime is not classified as variety
            if 'animation' in genres.lower() or 'animation' in keywords.lower() or meta.get('anime', False) or meta.get('mal_id'):
                return 405  # 動畫
            # Check for variety shows (綜藝)
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

    @staticmethod
    def _mediainfo_tracks_list(mi: dict[str, Any]) -> list[dict[str, Any]]:
        """Return mediainfo track list; normalize single dict to list (MediaInfo XML can emit one track as dict)."""
        if not mi:
            return []
        media = cast(dict[str, Any], mi.get('media', {}))
        raw = media.get('track', [])
        if isinstance(raw, dict):
            return [raw]
        return cast(list[dict[str, Any]], raw) if isinstance(raw, list) else []

    async def get_video_codec_id(self, meta: Meta) -> Optional[int]:
        """Get video codec ID for MTEAM form (returns integer ID)
        Based on videoCodecList.json: 1=H.264, 16=H.265/HEVC, 2=VC-1, 4=MPEG-2, 3=Xvid, 19=AV1, 21=VP8/9, 22=AVS
        Uses structured meta['mediainfo'] first; for BDMV (no mediainfo dict) falls back to parsing BDInfo/mediainfo text.
        """
        mi = cast(dict[str, Any], meta.get('mediainfo', {}))
        tracks = self._mediainfo_tracks_list(mi)
        video_tracks = [t for t in tracks if t.get('@type') == 'Video']
        if video_tracks:
            codec = str(video_tracks[0].get('Format', '')).upper()
            if 'HEVC' in codec or 'H.265' in codec or 'X265' in codec:
                return 16
            elif 'AVC' in codec or 'H.264' in codec or 'X264' in codec:
                return 1
            elif 'VC-1' in codec:
                return 2
            elif 'MPEG-2' in codec or 'MPEG2' in codec:
                return 4
            elif 'XVID' in codec:
                return 3
            elif 'AV1' in codec:
                return 19
            elif 'VP8' in codec or 'VP9' in codec:
                return 21
            elif 'AVS' in codec:
                return 22
            return None
        # Fallback: BDMV and others may have no meta['mediainfo']; parse BDInfo/mediainfo text
        mediainfo_text = await self.get_mediainfo_text(meta)
        video_id, _ = _parse_codec_ids_from_mediainfo_text(mediainfo_text)
        return video_id

    async def get_audio_codec_id(self, meta: Meta) -> Optional[int]:
        """Get audio codec ID for MTEAM form (returns integer ID)
        Based on audioCodecList.json: 6=AAC, 8=AC3, 3=DTS, 11=DTS-HD MA, 12=E-AC3, 13=E-AC3 Atoms,
        9=TrueHD, 10=TrueHD Atmos, 14=LPCM/PCM, 15=WAV, 1=FLAC, 2=APE, 4=MP2/3, 5=OGG, 7=Other
        Uses structured meta['mediainfo'] first; for BDMV (no mediainfo dict) falls back to parsing BDInfo/mediainfo text.
        """
        mi = cast(dict[str, Any], meta.get('mediainfo', {}))
        tracks = self._mediainfo_tracks_list(mi)
        audio_tracks = [t for t in tracks if t.get('@type') == 'Audio']
        if audio_tracks:
            codec = str(audio_tracks[0].get('Format', '')).upper()
            format_profile = str(audio_tracks[0].get('Format_Profile', '')).upper()
            if 'E-AC3' in codec and ('ATMOS' in format_profile or 'ATMOS' in codec):
                return 13
            elif 'E-AC3' in codec or 'DDP' in codec:
                return 12
            elif 'ATMOS' in format_profile or 'TRUEHD ATMOS' in codec:
                return 10
            elif 'TRUEHD' in codec:
                return 9
            elif 'DTS-HD' in codec or 'DTSHD' in codec or 'DTS-HD MA' in codec:
                return 11
            elif 'DTS:X' in format_profile or 'DTSX' in format_profile:
                return 3
            elif 'DTS' in codec:
                return 3
            elif 'LPCM' in codec or 'PCM' in codec:
                return 14
            elif 'WAV' in codec:
                return 15
            elif 'FLAC' in codec:
                return 1
            elif 'APE' in codec:
                return 2
            elif 'MP2' in codec or 'MP3' in codec:
                return 4
            elif 'OGG' in codec or 'VORBIS' in codec:
                return 5
            elif 'AC3' in codec or 'DD' in codec or 'DOLBY DIGITAL' in codec:
                return 8
            elif 'AAC' in codec:
                return 6
            return None
        # Fallback: parse BDInfo/mediainfo text (e.g. BDMV has no meta['mediainfo'])
        mediainfo_text = await self.get_mediainfo_text(meta)
        _, audio_id = _parse_codec_ids_from_mediainfo_text(mediainfo_text)
        return audio_id

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
            tracks = self._mediainfo_tracks_list(mi)
            for track in tracks:
                if track.get('@type') == "Text":
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
        tracks = self._mediainfo_tracks_list(mi)
        for track in tracks:
            if track.get('@type') == "Audio":
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
        """生成 descr 参数内容，结构参考 docs/mteam/desc.txt：海报 → 自定义说明 → 影片信息+简介 → 截图。
        注意：BDInfo/mediainfo 已通过接口单独传递（mediainfo 参数），不再添加到 desc 中。"""
        async with aiofiles.open(f"{meta['base_dir']}/tmp/{meta['uuid']}/DESCRIPTION.txt", encoding='utf-8') as base_file:
            base = await base_file.read()

        from src.bbcode import BBCODE
        common = COMMON(config=self.config)
        bbcode = BBCODE()

        # 1) 自定义说明（转自/制作说明）：DESCRIPTION.txt，保留 [color=red]/[color=blue] 等
        desc = base
        desc = bbcode.convert_code_to_quote(desc)
        desc = bbcode.convert_spoiler_to_hide(desc)
        desc = bbcode.convert_comparison_to_centered(desc, 1000)
        desc = desc.replace('[center]', '').replace('[/center]', '')
        desc = re.sub(r'\[url=([^\]]+)\]\[img\]([^\[]*?)\[/img\]\[/url\]', r'![](\1)', desc, flags=re.IGNORECASE | re.DOTALL)
        desc = re.sub(r'\[img\]([^\[]+?)\[/img\]', r'![](\1)', desc, flags=re.IGNORECASE)
        desc = re.sub(r'\[img=\d+\]([^\[]+?)\[/img\]', r'![](\1)', desc, flags=re.IGNORECASE)

        parts: list[str] = []

        # 2) 海报（ptgen 首图）
        ptgen_body = ""
        if int(meta.get('imdb_id', 0) or 0) != 0:
            ptgen = await common.ptgen(meta, self.ptgen_api, self.ptgen_retry)
            if ptgen.strip() != '':
                ptgen_markdown = re.sub(r'\[img\]([^\[]+?)\[/img\]', r'![](\1)', ptgen, flags=re.IGNORECASE)
                poster_match = re.match(r'^(\!\[\]\([^)]+\))\s*\n?(.*)', ptgen_markdown, re.DOTALL)
                if poster_match:
                    parts.append(poster_match.group(1).strip())
                    ptgen_body = poster_match.group(2).strip()
                else:
                    parts.append(ptgen_markdown.strip())
                parts.append("\n\n")

        # 3) 自定义说明（转自/制作说明，与 desc.txt 一致）
        if desc.strip():
            parts.append(desc.strip())
            parts.append("\n\n")

        # 4) 影片信息+简介（ptgen 正文：◎ 译名/片名/简介等）
        if ptgen_body:
            parts.append(ptgen_body)
            parts.append("\n\n")

        # 5) 截图
        # Note: BDInfo/mediainfo 已通过接口单独传递，不再添加到 desc 中
        images = cast(list[dict[str, Any]], meta.get('image_list', []))
        for each in range(len(images[:int(meta['screens'])])):
            img_url = images[each]['img_url']
            parts.append(f"![]({img_url})")

        if self.signature:
            parts.append("\n\n")
            parts.append(self.signature)

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
        For BDMV, use BD_FULL_00.txt (full BDInfo).
        For other types, use MI_FULL_00.txt (full MediaInfo) if present, else MEDIAINFO.txt.
        """
        if meta.get('bdinfo') is not None or meta.get('is_disc') == 'BDMV':
            full_bdinfo_path = f"{meta['base_dir']}/tmp/{meta['uuid']}/BD_FULL_00.txt"
            if os.path.exists(full_bdinfo_path):
                mi_path = full_bdinfo_path
                console.print(f"[green]Using full BDInfo file: {os.path.basename(full_bdinfo_path)}[/green]")
            else:
                mi_path = f"{meta['base_dir']}/tmp/{meta['uuid']}/BD_SUMMARY_00.txt"
                console.print(f"[yellow]BD_FULL_00.txt not found, falling back to BD_SUMMARY_00.txt[/yellow]")
        else:
            mi_full_path = f"{meta['base_dir']}/tmp/{meta['uuid']}/MI_FULL_00.txt"
            if os.path.exists(mi_full_path):
                mi_path = mi_full_path
                console.print(f"[green]Using full MediaInfo file: {os.path.basename(mi_full_path)}[/green]")
            else:
                mi_path = f"{meta['base_dir']}/tmp/{meta['uuid']}/MEDIAINFO.txt"
                console.print(f"[yellow]MI_FULL_00.txt not found, using MEDIAINFO.txt[/yellow]")
        
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
            await common.ptgen(meta, self.ptgen_api, self.ptgen_retry)
            if not meta.get('ptgen'):
                console.print("[red]Warning: ptgen() did not set meta['ptgen']![/red]")
                console.print(f"[red]imdb_id: {meta.get('imdb_id')}, ptgen_api: {self.ptgen_api}[/red]")

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

        # Build smallDescr：仅标题，可选类别（desc 中未必有字幕信息，不再从描述抽取拼接）
        if chinese_title:
            small_descr = chinese_title
            genre_value = genres[0] if genres and genres[0].strip() else ''
            if genre_value:
                small_descr += f" | 类别:{genre_value}"
        else:
            small_descr = str(meta.get('title', ''))
        
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
        # Priority: meta['douban_url'] > meta['douban_id'] > ptgen['douban']
        douban_url = ""
        
        # First, try to get from meta['douban_url']
        if meta.get('douban_url'):
            douban_url = meta.get('douban_url')
        # Second, try to build from meta['douban_id'] or meta['douban']
        elif meta.get('douban_id') or meta.get('douban'):
            douban_id = meta.get('douban_id') or meta.get('douban')
            if douban_id:
                douban_url = f"https://movie.douban.com/subject/{douban_id}/"
        # Finally, try to get from ptgen
        elif ptgen:
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
            try:
                data_obj = await self._request(url, data=data, files=files)
            except MTEAMRequestError as e:
                console.print(f"[red]Upload to MTEAM Failed: {e.message}[/red]")
                return False
            console.print("[green]Uploaded to MTEAM successfully[/green]")
            meta['tracker_status'][self.tracker]['status_message'] = "Upload successful"
            if isinstance(data_obj, dict):
                torrent_id = data_obj.get('id') or data_obj.get('torrentId')
                if torrent_id:
                    torrent_id_str = str(torrent_id)
                    meta['tracker_status'][self.tracker]['torrent_id'] = torrent_id_str
                    # Download the torrent file using the new credential mechanism
                    await self.download_new_torrent(torrent_id_str, torrent_path)
            return True
