from __future__ import annotations
import os
import time
from IPython.display import Image, SVG
import re
import tempfile
import contextlib
import sys
import uuid
from itertools import chain
from pathlib import Path
from typing import cast, Iterable 
from IPython.display import SVG, Image
from metakernel import MetaKernel, ProcessMetaKernel, pexpect
from metakernel.process_metakernel import TextOutput

from .exceptions import GnuplotError
from .replwrap import PROMPT_RE, PROMPT_REMOVE_RE, GnuplotREPLWrapper
from .statement import STMT
from .utils import get_version

IMG_COUNTER = "__gpk_img_index"
IMG_COUNTER_FMT = "%03d"
_TERMSPEC_SIZE_RE = re.compile(r"\bsize\s+(\d+)\s*,\s*(\d+)\b", re.IGNORECASE)
_SPLOT_CMD_RE = re.compile(r"^\s*(?:splot|splo|spl|sp)\b", re.IGNORECASE)

class GnuplotKernel(ProcessMetaKernel):
    """
    GnuplotKernel
    """
    _pending_unlink: list[Path] = []


    @staticmethod
    def _wait_nonzero(path, timeout=5.0, poll=0.01):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if path.stat().st_size > 0:
                    return True
            except FileNotFoundError:
                pass
            time.sleep(poll)
        return False

    @staticmethod
    def _termspec_with_min_size(termspec: str, min_w: int, min_h: int) -> str:
        """
        若 termspec 未指定 size，则插入 size min_w,min_h
        若指定了 size 但小于下限，则提升到下限
        其它情况保持不变
        """
        m = _TERMSPEC_SIZE_RE.search(termspec)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if w >= min_w and h >= min_h:
                return termspec
            return _TERMSPEC_SIZE_RE.sub(f"size {min_w}, {min_h}", termspec, count=1)

        parts = termspec.strip().split(None, 1)
        if not parts:
            return termspec
        term = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        return f"{term} size {min_w}, {min_h}" + (f" {rest}" if rest else "")



    @staticmethod
    def _looks_complete_bytes(data: bytes, fmt: str) -> bool:
        fmt = str(fmt).lower().lstrip(".")
        if fmt == "jpg":
            fmt = "jpeg"

        # JPEG: SOI + (last EOI close to end)
        if fmt == "jpeg":
            if not data.startswith(b"\xff\xd8"):
                return False
            i = data.rfind(b"\xff\xd9")
            return i != -1 and i >= max(0, len(data) - 64)

        # PNG: signature + IEND close to end
        if fmt == "png":
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                return False
            return data.rfind(b"IEND") >= max(0, len(data) - 64)

        # GIF: trailer 0x3B
        if fmt == "gif":
            if not (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
                return False
            return data.endswith(b"\x3b")

        # PDF: %%EOF near end
        if fmt == "pdf":
            tail = data[-2048:] if len(data) > 2048 else data
            return b"%%EOF" in tail

        # other formats: keep original behavior
        return True

    @staticmethod
    def _wait_size_stable(path: Path, timeout=20.0, poll=0.02, stable_rounds=5) -> int:
        """
        Wait until file size stops changing for `stable_rounds` consecutive polls.
        Returns the last observed size (may be 0 if timeout).
        """
        t0 = time.time()
        last = -1
        stable = 0
        last_size = 0
        while time.time() - t0 < timeout:
            try:
                sz = path.stat().st_size
            except FileNotFoundError:
                sz = 0

            last_size = sz
            if sz > 0 and sz == last:
                stable += 1
                if stable >= stable_rounds:
                    return sz
            else:
                stable = 0
                last = sz

            time.sleep(poll)
        return last_size



    # @staticmethod
    # def _looks_complete_bytes(data: bytes, fmt: str) -> bool:
    #     fmt = str(fmt).lower().lstrip(".")
    #     if fmt == "jpg":
    #         fmt = "jpeg"

    #     if fmt == "jpeg":
    #         # SOI 0xFFD8 ... EOI 0xFFD9（EOI 应出现在末尾附近）
    #         if len(data) < 4:
    #             return False
    #         if not data.startswith(b"\xff\xd8"):
    #             return False
    #         i = data.rfind(b"\xff\xd9")
    #         return i != -1 and i >= max(0, len(data) - 64)

    #     if fmt == "png":
    #         if not data.startswith(b"\x89PNG\r\n\x1a\n"):
    #             return False
    #         return data.rfind(b"IEND") >= max(0, len(data) - 64)

    #     if fmt == "gif":
    #         if not (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
    #             return False
    #         return data.endswith(b"\x3b")

    #     if fmt == "pdf":
    #         tail = data[-2048:] if len(data) > 2048 else data
    #         return b"%%EOF" in tail

    #     return True






    # @staticmethod
    # def _read_complete_bytes_retry(
    #     path: Path, 
    #     fmt: str | None = None,
    #     timeout: float = 20.0,
    #     poll: float = 0.01,
    # ) -> bytes:
    #     """
    #     Windows 上大图写入时，文件可能：
    #       - 被写端独占锁住（read 会 PermissionError/WinError 32）
    #       - size 非零但仍在增长（读到截断内容）
    #     这里用“读前 size == 读后 size 且 len(data) == size”作为完成判据。
    #     """
    #     t0 = time.time()
    #     last_exc: Exception | None = None
    #     delay = poll
    #     while time.time() - t0 < timeout:
    #         # try:
    #         #     target = path.stat().st_size
    #         # except FileNotFoundError:
    #         #     target = 0

    #         # if target <= 0:
    #         try:
    #             before = path.stat().st_size
    #         except FileNotFoundError:
    #             before = 0

    #         if before <= 0:
    #             time.sleep(poll)
    #             continue

    #         try:
    #             data = path.read_bytes()
    #             # 若仍在写入，常见现象是 len(data) < target
    #             # if len(data) == target:
    #             #     # 再确认一次 size 没继续变大（避免 race）
    #             #     stable = GnuplotKernel._wait_size_stable(path, timeout=timeout, poll=poll)
    #             #     if stable == len(data):
    #             #         return data

    #             try:
    #                 after = path.stat().st_size
    #             except FileNotFoundError:
    #                 after = 0

    #             # 写入完成且未增长，才认为完整
    #             if after == before and len(data) == after:
    #                 if fmt and not GnuplotKernel._looks_complete_bytes(data, fmt):
    #                     time.sleep(poll)
    #                     continue

    #                 # 再短暂确认一次 size 不会在“读完后”继续增长
    #                 stable = GnuplotKernel._wait_size_stable(
    #                         path,
    #                     timeout=min(0.6, timeout),
    #                     poll=poll,
    #                     stable_rounds=5,
    #                 )
    #                 if stable != after:
    #                     time.sleep(poll)
    #                     continue

    #                 return data
                
    #         except (PermissionError, OSError) as e:
    #             last_exc = e

    #         # time.sleep(poll)
    #         time.sleep(delay)
    #         # 指数退避，上限 50ms
    #         delay = min(delay * 1.6, 0.02)
 

    #     if last_exc:
    #         raise last_exc
    #     return b""



    @staticmethod
    def _read_complete_bytes_retry(
        path: Path,
        fmt: str | None = None,
        timeout: float = 20.0,
        poll: float = 0.01,
    ) -> bytes:
        """
        Windows 上大图写入时，文件可能：
        1) 被写端独占锁住（read 会 PermissionError/WinError 32）
        2) size 非零但仍在增长（读到截断内容）
        完成判据：
        1) 读前 size == 读后 size 且 len(data) == size
        2) 若提供 fmt，则满足对应格式的“完整性判据”（jpeg 要有 EOI）
        3) 读完后再确认一次 size 确实稳定（短暂等待）
        """
        t0 = time.time()
        last_exc: Exception | None = None
        delay = poll

        while time.time() - t0 < timeout:
            try:
                before = path.stat().st_size
            except FileNotFoundError:
                before = 0

            if before <= 0:
                time.sleep(poll)
                continue

            try:
                data = path.read_bytes()

                try:
                    after = path.stat().st_size
                except FileNotFoundError:
                    after = 0

                if after == before and len(data) == after:
                    if fmt and not GnuplotKernel._looks_complete_bytes(data, fmt):
                        time.sleep(poll)
                        continue

                    stable = GnuplotKernel._wait_size_stable(
                        path,
                        timeout=min(0.6, timeout),
                        poll=poll,
                        stable_rounds=5,
                    )
                    if stable != after:
                        time.sleep(poll)
                        continue

                    return data

            except (PermissionError, OSError) as e:
                last_exc = e

            time.sleep(delay)
            delay = min(delay * 1.6, 0.02)

        if last_exc:
            raise last_exc
        return b""
        












    @staticmethod
    def _read_bytes_retry(path, timeout=5.0, poll=0.02):
        t0 = time.time()
        last_exc = None
        while time.time() - t0 < timeout:
            try:
                data = path.read_bytes()
                if data:
                    return data
            except (PermissionError, OSError) as e:
                last_exc = e
            time.sleep(poll)
        if last_exc:
            raise last_exc
        return b""
    









    implementation = "Gnuplot Kernel"
    implementation_version = get_version("gnuplot_kernel")
    language = "gnuplot"
    _banner = "Gnuplot Kernel"
    language_version = "5.0"  # pyright: ignore[reportAssignmentType,reportIncompatibleMethodOverride]
    language_info = {
        "mimetype": "text/x-gnuplot",
        "name": "gnuplot",
        "file_extension": ".gp",
        "codemirror_mode": "Octave",
        "help_links": MetaKernel.help_links,
    }
    kernel_json = {
        "argv": [
            sys.executable,
            "-m",
            "gnuplot_kernel",
            "-f",
            "{connection_file}",
        ],
        "display_name": "gnuplot",
        "language": "gnuplot",
        "name": "gnuplot",
    }

    inline_plotting = True
    reset_code = ""
    _first = True
    _image_files: list[Path] = []
    _error = False

    wrapper: GnuplotREPLWrapper
    _bad_prompts: set = set()

    # def check_prompt(self):
    #     """
    #     Print warning if the prompt looks bad

    #     A bad prompt is one that does not contain the string 'gnuplot>'.
    #     The warning is printed once per bad prompt.
    #     """
    #     prompt = cast("str", self.wrapper.prompt)
    #     if "gnuplot>" not in prompt and prompt not in self._bad_prompts:
    #         print(f"Warning: The prompt is currently set to '{prompt}'")
    #         self._bad_prompts.add(prompt)


    def _iter_inline_candidates(self) -> list[Path]:
        """
        Return inline image files currently present (sorted).
        The existing code likely already has iter_image_files(); this helper is
        only for the quiescence wait to repeatedly sample the directory.
        """
        return list(self.iter_image_files())

    def _wait_inline_quiescence(
            self,
        timeout: float = 2.0,
        poll: float = 0.01,
        settle: float = 0.08,
    ) -> None:
        """
        Wait until inline output files stop appearing/changing.
        We treat the output as 'ready' when:
          - number of candidate files stops increasing, AND
          - newest mtime stops changing for `settle` seconds.
        This prevents missing plots in a single cell with multiple `plot` lines,
        especially when notebook 'Run All' removes inter-cell idle gaps.
        """
        t0 = time.time()
        last_count = -1
        last_newest_mtime = -1.0
        stable_since = None  # type: float | None

        while time.time() - t0 < timeout:
            files = self._iter_inline_candidates()
            count = len(files)
            newest_mtime = -1.0
            if files:
                # newest mtime among candidates
                try:
                    newest_mtime = max(p.stat().st_mtime for p in files)
                except FileNotFoundError:
                    newest_mtime = -1.0

            changed = (count != last_count) or (newest_mtime != last_newest_mtime)

            if changed:
                last_count = count
                last_newest_mtime = newest_mtime
                stable_since = None
            else:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= settle:
                    return

            time.sleep(poll)



    def check_prompt(self):
        prompt = cast("str", self.wrapper.prompt)
        if prompt == "__GPK_READY__":
            return
        if "gnuplot>" not in prompt and prompt not in self._bad_prompts:
            print(f"Warning: The prompt is currently set to '{prompt}'")
            self._bad_prompts.add(prompt)
    def do_execute_direct(self, code, silent=False):
        # We wrap the real function so that gnuplot_kernel can
        # give a message when an exception occurs. Without
        # this, an exception happens silently
        try:
            return self._do_execute_direct(code)
        except Exception as err:
            print(f"Error: {err}")
            raise err

    def _do_execute_direct(self, code: str) -> TextOutput | None:
        """
        Execute gnuplot code
        """
        if self._first:
            self._first = False
            self.handle_plot_settings()

        if self.inline_plotting:
            code = self.add_inline_image_statements(code)

        success = True

        try:
            result = super().do_execute_direct(code, silent=True)
        except GnuplotError as e:
            result = TextOutput(e.message)
            success = False

        if self.reset_code:
            super().do_execute_direct(self.reset_code, silent=True)

        if self.inline_plotting:
            if success:
                self.display_images()
            self.delete_image_files()

        self.check_prompt()

        # No empty strings
        return result if (result and result.output) else None

    def add_inline_image_statements(self, code: str) -> str:
        """
        Add 'set output ...' before every plotting statement

        This is what powers inline plotting
        """

        # "set output sprintf('foobar.%d.png', counter);"
        # "counter=counter+1"


        settings = self.plot_settings
        base_termspec = str(settings.get("termspec", "")).strip()
        fmt = str(settings.get("format", "png")).lower().lstrip(".")
        if fmt == "jpg":
            fmt = "jpeg"

        restore_termspec_pending = False

        def maybe_restore_terminal(lines):
            nonlocal restore_termspec_pending
            if restore_termspec_pending and base_termspec:
                lines.append(f"set terminal {base_termspec}")
            restore_termspec_pending = False

        def set_output_inline(lines, stmt):
            nonlocal restore_termspec_pending
            # 若上一幅图临时改过 terminal，这里先恢复，确保不影响后续 plot
            maybe_restore_terminal(lines)

            # splot 临时放大画布，避免内容被裁掉
            # 只对常见 inline 位图格式启用，且仅当 base_termspec 存在
            if base_termspec and fmt in ("jpeg", "png", "svg"):
                if _SPLOT_CMD_RE.match(str(stmt)):
                    big_termspec = self._termspec_with_min_size(base_termspec, 1024, 768)
                    if big_termspec != base_termspec:
                        lines.append(f"set terminal {big_termspec}")
                        restore_termspec_pending = True
        # def set_output_inline(lines):




            tpl = self.get_image_filename()
            if tpl:
                cmd = (
                    f"set output sprintf('{tpl}', {IMG_COUNTER});"
                    f"{IMG_COUNTER}={IMG_COUNTER}+1"
                )
                lines.append(cmd)

        # We automatically create an output file for the following
        # cases if the user has not created one.
        #    - before every plot statement that is not in a
        #      multiplot block
        #    - before every multiplot block

        lines = []
        sm = StateMachine()
        is_joined_stmt = False
        for line in code.splitlines():
            stmt = STMT(line)
            sm.transition(stmt)
            add_inline_plot = (
                sm.prev_cur
                in (("none", "plot"), ("none", "multiplot"), ("plot", "plot"))
                and not is_joined_stmt
            )
            if add_inline_plot:
                set_output_inline(lines, stmt)

            lines.append(stmt)
            is_joined_stmt = stmt.strip().endswith("\\")

        # Make gnuplot flush the output
        if not lines[-1].endswith("\\"):
            lines.append("unset output")
        # 若最后一幅图是 splot 并临时改过 terminal，这里恢复
        if restore_termspec_pending and base_termspec:
            lines.append(f"set terminal {base_termspec}")



        code = "\n".join(lines)
        return code

    # def get_image_filename(self):
    #     """
    #     Create file to which gnuplot will write the plot

    #     Returns the filename.
    #     """
    #     # we could use tempfile.NamedTemporaryFile but we do not
    #     # want to create the file, gnuplot will create it.
    #     # Later on when we check if the file exists we know
    #     # whodunnit.
    #     fmt = self.plot_settings["format"]
    #     filename = Path(
    #         f"/tmp/gnuplot-inline-{uuid.uuid1()}.{IMG_COUNTER_FMT}.{fmt}"
    #     )
    #     self._image_files.append(filename)
    #     return filename
    def get_image_filename(self):
        fmt = self.plot_settings["format"]
        tmpdir = Path(tempfile.gettempdir())
        filename = tmpdir / f"gnuplot-inline-{uuid.uuid1()}.{IMG_COUNTER_FMT}.{fmt}"
        # print(filename)
        self._image_files.append(filename)
        return filename.as_posix()
    

    def iter_image_files(self):
        """
        Iterate over the image files
        """
        it = chain(
            *[
                sorted(f.parent.glob(f.name.replace(IMG_COUNTER_FMT, "*")))
                for f in self._image_files
            ]
        )
        return it

    # def display_images(self):
    #     """
    #     Display images if gnuplot wrote to them
    #     """
    #     settings = self.plot_settings
    #     if self.inline_plotting:
    #         _Image = SVG if settings["format"] == "svg" else Image
    #     else:
    #         return

    #     for filename in self.iter_image_files():
    #         try:
    #             size = filename.stat().st_size
    #         except FileNotFoundError:
    #             size = 0

    #         if not size:
    #             msg = (
    #                 "Failed to read and display image file from gnuplot."
    #                 "Possibly:\n"
    #                 "1. You have plotted to a non interactive terminal.\n"
    #                 "2. You have an invalid expression."
    #             )
    #             print(msg)
    #             continue

    #         im = _Image(str(filename))
    #         self.Display(im)




    def display_images(self):
        # Key fix: do not start reading before gnuplot finishes emitting
        # all images for this cell.
        if os.name == "nt" and self.inline_plotting:  
            files0 = self._iter_inline_candidates()
            max_sz = 0
            for p in files0:
                try:
                    max_sz = max(max_sz, p.stat().st_size)
                except FileNotFoundError:
                    pass
            if max_sz >= 10_000_000:
                self._wait_inline_quiescence(timeout=8.0, poll=0.02, settle=0.18)
            else:
                self._wait_inline_quiescence(timeout=2.0, poll=0.01, settle=0.08)


                # 非阻塞清理上一轮未删掉的文件
        if os.name == "nt" and self._pending_unlink:
            keep: list[Path] = []
            for p in self._pending_unlink:
                try:
                    p.unlink()
                except (FileNotFoundError, PermissionError):
                    keep.append(p)
            self._pending_unlink = keep




        settings = self.plot_settings
        if not self.inline_plotting:
            return

        fmt = str(settings.get("format", "png")).lower().lstrip(".")
        if fmt == "jpg":
            fmt = "jpeg"

        for filename in self.iter_image_files():
            
            # if os.name == "nt":
            #     self._wait_nonzero(filename, timeout=10.0, poll=0.02)

            if os.name == "nt":
                # 大图：等 size 稳定，而不是只等到“非零”
                self._wait_size_stable(filename, timeout=10.0, poll=0.02, stable_rounds=5)



            try:
                size = filename.stat().st_size
            except FileNotFoundError:
                size = 0

            if not size:
                msg = (
                    "Failed to read and display image file from gnuplot.Possibly:\n"
                    "1. You have plotted to a non interactive terminal.\n"
                    "2. You have an invalid expression."
                )
                print(msg)
                continue





            # if fmt == "svg":
            #     data = filename.read_text(encoding="utf-8", errors="replace")
            #     self.Display(SVG(data=data))
 
            # else:
            #     data = self._read_bytes_retry(filename, timeout=10.0, poll=0.02)
            #     self.Display(Image(data=data, format=fmt))

            try:
                if fmt == "svg":
                    data = filename.read_text(encoding="utf-8", errors="replace")
                    self.Display(SVG(data=data))
                else:
                    # 关键：读“完整文件”，避免截断 + WinError 32 直接冒泡
                    if os.name == "nt":
                        # data = self._read_complete_bytes_retry(filename, timeout=10.0, poll=0.01)
                        # data = self._read_complete_bytes_retry(filename, timeout=30.0, poll=0.02)


                                                # 自适应：文件越大，允许更久的写入完成时间
                        try:
                            sz0 = filename.stat().st_size
                        except FileNotFoundError:
                            sz0 = 0
                        if sz0 >= 20_000_000:       # 约 20MB，典型大图
                            timeout, poll = 20.0, 0.02
                        elif sz0 >= 2_000_000:      # 中等
                            timeout, poll = 8.0, 0.01
                        else:                        # 小图
                            timeout, poll = 2.0, 0.005
                        # data = self._read_complete_bytes_retry(filename, timeout=timeout, poll=poll)
                        data = self._read_complete_bytes_retry(filename, fmt=fmt, timeout=timeout, poll=poll)



                    else:
                        data = self._read_bytes_retry(filename, timeout=10.0, poll=0.02)
                    self.Display(Image(data=data, format=fmt))
            except (PermissionError, OSError) as e:
                # 不把异常抛到 do_execute_direct 外层，避免额外的 “Error: ...” 输出
                print(f"Error: {e}")
                continue




    def delete_image_files(self):
        """
        Delete the image files
        """
        # After display_images(), the real images are
        # no longer required.
        for filename in self.iter_image_files():
            # with contextlib.suppress(FileNotFoundError):
            #     filename.unlink()
             
            if os.name == "nt":
                # t0 = time.time()
                # while True:
                #     try:
                #         filename.unlink()
                #         break
                #     except FileNotFoundError:
                #         break
                #     except PermissionError:
                #         if time.time() - t0 > 2.0:
                #             break
                #         time.sleep(0.02)




                try:
                    filename.unlink()
                except (FileNotFoundError, PermissionError):
                    self._pending_unlink.append(filename)


            else:
                with contextlib.suppress(FileNotFoundError):
                    filename.unlink()

        self._image_files = []

    # def makeWrapper(self):
    #     """
    #     Start gnuplot and return wrapper around the REPL
    #     """
    #     if pexpect.which("gnuplot"):
    #         program = "gnuplot"
    #     elif pexpect.which("gnuplot.exe"):
    #         program = "gnuplot.exe"
    #     else:
    #         raise Exception("gnuplot not found.")

    #     # We don't want help commands getting stuck,
    #     # use a non interactive PAGER
    #     if pexpect.which("env") and pexpect.which("cat"):
    #         command = "env PAGER=cat {}".format(program)
    #     else:
    #         command = program

    #     wrapper = GnuplotREPLWrapper(
    #         cmd_or_spawn=command,
    #         prompt_regex=PROMPT_RE,
    #         prompt_change_cmd=None,
    #     )
    #     # No sleeping before sending commands to gnuplot
    #     wrapper.child.delaybeforesend = 0
    #     return wrapper
    def makeWrapper(self):
        if pexpect.which("gnuplot"):
            program = "gnuplot"
        elif pexpect.which("gnuplot.exe"):
            program = "gnuplot.exe"
        else:
            raise Exception("gnuplot not found.")

        if pexpect.which("env") and pexpect.which("cat"):
            command = "env PAGER=cat {}".format(program)
        else:
            command = program

        if os.name == "nt":
            READY = "__GPK_READY__"
            wrapper = GnuplotREPLWrapper(
                cmd_or_spawn=command,
                prompt_regex=re.compile(re.escape(READY)),
                prompt_change_cmd=None,
                continuation_prompt_regex=re.compile(r"(?!)"),
                prompt_emit_cmd=f'print "{READY}"',
            )
        else:
            wrapper = GnuplotREPLWrapper(
                cmd_or_spawn=command,
                prompt_regex=PROMPT_RE,
                prompt_change_cmd=None,
            )

        wrapper.child.delaybeforesend = 0
        return wrapper
    def do_shutdown(self, restart):
        """
        Exit the gnuplot process and any other underlying stuff
        """
        self.wrapper.exit()
        super().do_shutdown(restart)

    def get_kernel_help_on(self, info, level=0, none_on_fail=False):
        obj = info.get("help_obj", "")
        if not obj or len(obj.split()) > 1:
            return None if none_on_fail else ""
        res = cast("TextOutput", self.do_execute_direct("help %s" % obj))
        text = PROMPT_REMOVE_RE.sub("", res.output)
        self.check_prompt()
        return text

    def reset_image_counter(self):
        # Incremented after every plot image, and used in the
        # plot image filename. Makes plotting in loops do_for
        # loops work
        cmd = f"{IMG_COUNTER}=0"
        self.do_execute_direct(cmd)

    # def handle_plot_settings(self):
    #     """
    #     Handle the current plot settings

    #     This is used by the gnuplot line magic. The plot magic
    #     is innadequate.
    #     """
    #     settings = self.plot_settings
    #     if "termspec" not in settings or not settings["termspec"]:
    #         settings["termspec"] = 'pngcairo size 385, 256 font "Arial,10"'
    #     if "format" not in settings or not settings["format"]:
    #         settings["format"] = "png"

    #     self.inline_plotting = settings["backend"] == "inline"

    #     cmd = "set terminal {}".format(settings["termspec"])
    #     self.do_execute_direct(cmd)
    #     self.reset_image_counter()



    def handle_plot_settings(self):
        settings = self.plot_settings

        if "termspec" not in settings or not settings["termspec"]:
            if os.name == "nt":
                settings["termspec"] = 'png size 385, 256'
            else:
                settings["termspec"] = 'pngcairo size 385, 256 font "Arial,10"'

        if "format" not in settings or not settings["format"]:
            settings["format"] = "png"

        self.inline_plotting = settings["backend"] == "inline"
        cmd = "set terminal {}".format(settings["termspec"])
        self.do_execute_direct(cmd)
        self.reset_image_counter()
 




    # def handle_plot_settings(self): 
    #     settings = self.plot_settings
    #     if "termspec" not in settings or not settings["termspec"]:
    #         settings["termspec"] = 'pngcairo size 385, 256 font "Arial,10"'
    #     if "format" not in settings or not settings["format"]:
    #         settings["format"] = "png" 
    #     # settings = self.plot_settings

    #     # if "format" not in settings or not settings["format"]:
    #     #     settings["format"] = "png"

    #     # fmt = str(settings["format"]).lower().lstrip(".")
    #     # if fmt == "jpg":
    #     #     fmt_for_term = "jpeg"
    #     # else:
    #     #     fmt_for_term = fmt
    #     # if "termspec" not in settings or not settings["termspec"]:
    #     #     if os.name == "nt":
    #     #         if fmt_for_term == "png":
    #     #             settings["termspec"] = "png size 385, 256"
    #     #         elif fmt_for_term == "jpeg":
    #     #             settings["termspec"] = "jpeg size 385, 256"
    #     #         elif fmt_for_term == "svg":
    #     #             settings["termspec"] = "svg size 385, 256"
    #     #         else:
    #     #             settings["termspec"] = "png size 385, 256"
    #     #     else:
    #     #         if fmt_for_term == "png":
    #     #             settings["termspec"] = 'pngcairo size 385, 256 font "Arial,10"'
    #     #         elif fmt_for_term == "jpeg":
    #     #             settings["termspec"] = 'jpeg size 385, 256'
    #     #         elif fmt_for_term == "svg":
    #     #             settings["termspec"] = 'svg size 385, 256'
    #     #         else:
    #     #             settings["termspec"] = 'pngcairo size 385, 256 font "Arial,10"'

    #     self.inline_plotting = settings["backend"] == "inline"
    #     cmd = "set terminal {}".format(settings["termspec"])
    #     self.do_execute_direct(cmd)
    #     self.reset_image_counter()










class StateMachine:
    """
    Track context given gnuplot statements

    This is used to help us tell when to inject commands (i.e. set output)
    that for inline plotting in the notebook.
    """

    states = ["none", "plot", "output", "multiplot", "output_multiplot"]
    previous = "none"
    _current = "none"

    @property
    def prev_cur(self):
        return (self.previous, self.current)

    @property
    def current(self):
        return self._current

    @current.setter
    def current(self, value):
        self.previous = self._current
        self._current = value

    def transition(self, stmt):
        lookup = {
            s: getattr(self, f"transition_from_{s}") for s in self.states
        }
        _transition = lookup[self.current]
        self.previous = self._current
        return _transition(stmt)

    def transition_from_plot(self, stmt):
        if self.current == "output":
            self.current = "none"
        elif self.current == "plot":
            if stmt.is_plot():
                self.current = "plot"
            elif stmt.is_set_output():
                self.current = "output"
            else:
                self.current = "none"

    def transition_from_none(self, stmt):
        if stmt.is_plot():
            self.current = "plot"
        elif stmt.is_set_output():
            self.current = "output"
        elif stmt.is_set_multiplot():
            self.current = "multiplot"

    def transition_from_output(self, stmt):
        if stmt.is_plot():
            self.current = "plot"
        elif stmt.is_set_multiplot():
            self.current = "output_multiplot"
        elif stmt.is_unset_output():
            self.current = "none"

    def transition_from_multiplot(self, stmt):
        if stmt.is_unset_multiplot():
            self.current = "none"

    def transition_from_output_multiplot(self, stmt):
        if stmt.is_unset_multiplot():
            self.previous = self.current
            self.current = "output"
