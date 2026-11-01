"""Create torrent metadata for our generated video, without trackers or public peers."""
from pathlib import Path
import libtorrent as lt

root = Path(__file__).resolve().parent / 'fixtures'
video = root / 'torrent-test.mp4'
files = lt.file_storage()
files.add_file(video.name, video.stat().st_size)
torrent = lt.create_torrent(files, 16 * 1024, lt.create_torrent.v1_only)
torrent.set_creator('KinoSfera local playback test')
lt.set_piece_hashes(torrent, str(root))
(root / 'torrent-test.torrent').write_bytes(lt.bencode(torrent.generate()))
print('Created local-only torrent fixture:', video.stat().st_size, 'bytes')
