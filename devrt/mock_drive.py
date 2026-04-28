"""
Plugin Dev Runtime — Mock Drive。

内存中的虚拟文件系统，支持 list_files / rename。
rename 后内存中真正改名，使预览和执行结果一致。
"""

from typing import List, Optional

from devrt.stubs import FileInfo


# ── 默认文件列表 ──

DEFAULT_FILES = [
    # 根目录下的文件夹
    FileInfo(id="d1", name="动漫", size=0, is_dir=True, parent_id="0"),
    FileInfo(id="d2", name="电影", size=0, is_dir=True, parent_id="0"),

    # 动漫目录下的文件
    FileInfo(id="f1", name="[BDMV] 灼眼的夏娜 S01E01.mkv", size=1_500_000_000, is_dir=False, parent_id="d1"),
    FileInfo(id="f2", name="[BDMV] 灼眼的夏娜 S01E02.mkv", size=1_400_000_000, is_dir=False, parent_id="d1"),
    FileInfo(id="f3", name="[BDMV] 灼眼的夏娜 S01E03.mkv", size=1_350_000_000, is_dir=False, parent_id="d1"),
    FileInfo(id="f4", name="[SubGroup] 进击的巨人 EP01 [1080p].mp4", size=800_000_000, is_dir=False, parent_id="d1"),
    FileInfo(id="f5", name="[SubGroup] 进击的巨人 EP02 [1080p].mp4", size=750_000_000, is_dir=False, parent_id="d1"),
    FileInfo(id="f6", name="[SubGroup] 进击的巨人 EP03 [1080p].mp4", size=780_000_000, is_dir=False, parent_id="d1"),

    # 电影目录下的文件
    FileInfo(id="f7", name="星际穿越.2014.BluRay.1080p.mkv", size=4_200_000_000, is_dir=False, parent_id="d2"),
    FileInfo(id="f8", name="盗梦空间.2010.BluRay.1080p.mkv", size=3_800_000_000, is_dir=False, parent_id="d2"),
    FileInfo(id="f9", name="千与千寻.2001.BluRay.1080p.mkv", size=2_100_000_000, is_dir=False, parent_id="d2"),

    # 根目录下的零散文件
    FileInfo(id="f10", name="README.txt", size=1024, is_dir=False, parent_id="0"),
    FileInfo(id="f11", name="notes.md", size=2048, is_dir=False, parent_id="0"),
]


class MockDrive:
    """
    内存虚拟文件系统。

    提供 list_files / rename 方法，兼容 DriveCat DriveProxy 接口。
    rename 后内存中真正改名，使预览结果和执行结果一致。
    """

    def __init__(self, files: Optional[List[FileInfo]] = None):
        # 深拷贝默认列表，防止跨测试污染
        if files is not None:
            self._files = list(files)
        else:
            self._files = [
                FileInfo(
                    id=f.id, name=f.name, size=f.size,
                    is_dir=f.is_dir, parent_id=f.parent_id,
                    modified_time=f.modified_time,
                )
                for f in DEFAULT_FILES
            ]

    async def list_files(self, parent_id: str) -> List[FileInfo]:
        """列出指定目录下的文件和文件夹。"""
        return [f for f in self._files if f.parent_id == parent_id]

    async def rename(self, file_id: str, new_name: str) -> bool:
        """重命名文件（内存中真正改名）。"""
        f = next((f for f in self._files if f.id == file_id), None)
        if f is None:
            return False
        f.name = new_name
        return True

    async def mkdir(self, parent_id: str, name: str) -> FileInfo:
        """创建目录。"""
        new_id = f"d{len([f for f in self._files if f.is_dir]) + 1}"
        new_dir = FileInfo(id=new_id, name=name, size=0, is_dir=True, parent_id=parent_id)
        self._files.append(new_dir)
        return new_dir

    def get_file(self, file_id: str) -> Optional[FileInfo]:
        """按 ID 获取文件信息。"""
        return next((f for f in self._files if f.id == file_id), None)

    def to_dict_list(self, parent_id: str) -> list:
        """返回适合 JSON 序列化的文件列表。"""
        return [
            {
                "id": f.id,
                "name": f.name,
                "size": f.size,
                "is_dir": f.is_dir,
            }
            for f in self._files
            if f.parent_id == parent_id
        ]
