"""Open a library location from its settings string.

=====================================  =============================================
``library:`` value                     Meaning
=====================================  =============================================
``C:/Users/me/Papers`` or ``~/Papers``  folder on this computer
``{onedrive}/PhD/Literature``           folder inside the OneDrive desktop-sync folder
                                        (also ``{gdrive}``, ``{dropbox}``, ``{icloud}``, ``{box}``)
``dropbox:/Research/Literature``        Dropbox API
``gdrive:/Research/Literature``         Google Drive API (path from My Drive)
``gdrive:id:<folderId>``                Google Drive API (folder ID, shared drives too)
``onedrive:/Documents/Literature``      OneDrive / SharePoint personal drive via Microsoft Graph
``rclone:<remote>:<path>``              anything rclone supports (Box, pCloud, MEGA, S3, Nextcloud...)
=====================================  =============================================
"""

from __future__ import annotations

from litsuite.storage.base import LibraryFile, Storage, folder_allowed  # noqa: F401
from litsuite.storage.local import LocalStorage

__all__ = ["LibraryFile", "Storage", "folder_allowed", "LocalStorage", "open_storage"]


def open_storage(spec: str, settings=None) -> Storage:
    cloud = (settings.data.get("cloud") if settings else None) or {}
    spec = str(spec)
    if spec.startswith("dropbox:"):
        from litsuite.storage.dropbox_backend import DropboxStorage

        return DropboxStorage(spec[len("dropbox:"):], (cloud.get("dropbox") or {}).get("app_key", ""))
    if spec.startswith("gdrive:"):
        from litsuite.storage.gdrive import GoogleDriveStorage

        return GoogleDriveStorage(spec[len("gdrive:"):], (cloud.get("gdrive") or {}).get("client_secret_file", ""))
    if spec.startswith("onedrive:"):
        from litsuite.storage.onedrive import OneDriveStorage

        od = cloud.get("onedrive") or {}
        return OneDriveStorage(spec[len("onedrive:"):], od.get("client_id", ""), od.get("tenant", "common"))
    if spec.startswith("rclone:"):
        from litsuite.storage.rclone import RcloneStorage

        return RcloneStorage(spec[len("rclone:"):])
    return LocalStorage(spec)
