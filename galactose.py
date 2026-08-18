import struct
import sys
import argparse
import tkinter as tk
import time
import threading
import subprocess
import os
import socket
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Tuple

# Sound handling for macOS systems
def play_tone(frequency, duration):
    if frequency <= 0:
        time.sleep(duration / 1000.0)
        return

    duration_sec = duration / 1000.0
    try:
        if sys.platform == "darwin":
            # macOS: Using Sox (if installed) or a silent sleep if not
            subprocess.Popen(["play", "-n", "synth", str(duration_sec), "sine", str(frequency)], 
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "win32":
            import winsound
            winsound.Beep(int(frequency), int(duration))
        else:
            subprocess.Popen(["beep", "-f", str(frequency), "-l", str(duration)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        time.sleep(duration_sec)

# ============================================================================
# GSDX Disk Image Format (Galactose Sparse Dynamic eXtended)
# ============================================================================

class GSDXHeader:
    """GSDX (Galactose Sparse Dynamic eXtended) disk image header parser/creator
    
    The GSDX format improves upon the original .gsd (raw disk image) by supporting:
    - Sparse allocation (only stores non-zero blocks)
    - Dynamic sizing (grows as data is added)
    - Metadata tracking (size, block allocation table)
    - Backward compatibility (can read legacy .gsd files)
    
    Structure:
        Header (128 bytes):
            - Magic: "GSDX" (4 bytes)
            - Version: uint16 (0x0001)
            - Flags: uint16
            - Max Theoretical Size: uint64
            - Current Allocated Size: uint64  
            - Used Data Size: uint64
            - Block Size: uint32 (default 4096)
            - Block Count: uint32
            - Reserved: 80 bytes
        
        Block Allocation Table (BAT):
            - Per block: uint64 offset (0 = unallocated)
        
        Data Blocks:
            - Only allocated blocks stored here
            - Unallocated blocks read as zeros
    """
    
    MAGIC = b'GSDX'
    VERSION = 0x0001
    HEADER_SIZE = 128
    
    # Flag bits
    FLAG_COMPRESSED = 0x0001
    FLAG_CHECKSUM = 0x0002
    _FLAG_ENCRYPTED_META = 0x0004
    
    def __init__(self):
        self.version = self.VERSION
        self.flags = 0
        self.max_size = 0          # 0 = unlimited
        self.allocated_size = 0
        self.used_data_size = 0
        self.block_size = 4096     # Default 4KB blocks
        self.block_count = 0
        self.bat: List[int] = []   # Block Allocation Table
        self._data_offset = 0      # Where data blocks start
    
    @classmethod
    def from_file(cls, path: Path) -> Optional['GSDXHeader']:
        """Parse GSDX header from file. Returns None if not a valid GSDX file."""
        try:
            with open(path, 'rb') as f:
                magic = f.read(4)
                if magic != cls.MAGIC:
                    return None
                
                header = cls()
                header.version = struct.unpack('<H', f.read(2))[0]
                header.flags = struct.unpack('<H', f.read(2))[0]
                header.max_size = struct.unpack('<Q', f.read(8))[0]
                header.allocated_size = struct.unpack('<Q', f.read(8))[0]
                header.used_data_size = struct.unpack('<Q', f.read(8))[0]
                header.block_size = struct.unpack('<I', f.read(4))[0]
                header.block_count = struct.unpack('<I', f.read(4))[0]
                
                # Skip reserved (80 bytes)
                f.read(80)
                
                header._data_offset = cls.HEADER_SIZE + (header.block_count * 8)
                
                # Read Block Allocation Table
                if header.block_count > 0:
                    bat_data = f.read(header.block_count * 8)
                    header.bat = list(struct.unpack(f'<{header.block_count}Q', bat_data))
                
                return header
        except Exception as e:
            print(f"[GSDX] Error parsing header: {e}")
            return None
    
    @classmethod
    def create(cls, max_size_mb: int = 0, block_size: int = 4096) -> 'GSDXHeader':
        """Create a new empty GSDX header."""
        header = cls()
        header.max_size = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0
        header.block_size = block_size
        header._data_offset = cls.HEADER_SIZE  # BAT starts empty
        return header
    
    def to_bytes(self) -> bytes:
        """Serialize header to bytes for writing to file."""
        data = bytearray()
        data.extend(self.MAGIC)                         # 4 bytes
        data.extend(struct.pack('<H', self.version))     # 2 bytes
        data.extend(struct.pack('<H', self.flags))       # 2 bytes
        data.extend(struct.pack('<Q', self.max_size))     # 8 bytes
        data.extend(struct.pack('<Q', self.allocated_size))  # 8 bytes
        data.extend(struct.pack('<Q', self.used_data_size))  # 8 bytes
        data.extend(struct.pack('<I', self.block_size))  # 4 bytes
        data.extend(struct.pack('<I', self.block_count)) # 4 bytes
        data.extend(b'\x00' * 80)                       # 80 bytes reserved
        
        # Write BAT
        if self.bat:
            data.extend(struct.pack(f'<{len(self.bat)}Q', *self.bat))
        
        return bytes(data)
    
    def get_block_offset(self, block_index: int) -> int:
        """Get file offset for a data block, or 0 if unallocated."""
        if block_index < len(self.bat):
            return self.bat[block_index]
        return 0
    
    def allocate_block(self, block_index: int, file_offset: int):
        """Record that a block has been allocated at the given offset."""
        while len(self.bat) <= block_index:
            self.bat.append(0)
        self.bat[block_index] = file_offset
        self.block_count = len(self.bat)


class GSDXDiskImage:
    """Handler for GSDX disk images with sparse allocation support.
    
    Provides transparent read/write access to GSDX files,
    handling sparse block allocation automatically.
    """
    
    def __init__(self, path: Path, create_new: bool = False, max_size_mb: int = 512):
        self.path = path
        self.header: Optional[GSDXHeader] = None
        self._file = None
        self._blocks_written: Dict[int, bytes] = {}
        
        if create_new:
            self._create_new(max_size_mb)
        else:
            self._open_existing()
    
    def _create_new(self, max_size_mb: int):
        """Create a new empty GSDX file."""
        self.header = GSDXHeader.create(max_size_mb=max_size_mb)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(self.path, 'wb') as f:
            f.write(self.header.to_bytes())
        
        print(f"[GSDX] Created new image: {self.path}")
        print(f"       Max size: {max_size_mb}MB" if max_size_mb else "       Max size: unlimited")
    
    def _open_existing(self):
        """Open existing GSDX file or fall back to raw .gsd mode."""
        if self.path.exists():
            self.header = GSDXHeader.from_file(self.path)
            if self.header:
                print(f"[GSDX] Opened dynamic image: {self.path}")
                print(f"       Allocated: {self.header.allocated_size // 1024 // 1024}MB")
                print(f"       Used: {self.header.used_data_size // 1024}KB")
            else:
                print(f"[GSDX] Not a GSDX file, treating as raw image: {self.path}")
        else:
            print(f"[GSDX] File does not exist: {self.path}")
    
    def is_gsdx(self) -> bool:
        """Check if this is a valid GSDX file."""
        return self.header is not None
    
    def read(self, offset: int, size: int) -> bytes:
        """Read data from the GSDX image, handling sparse blocks."""
        if not self.header or not self.path.exists():
            return b'\x00' * size
        
        result = bytearray(size)
        bytes_read = 0
        
        while bytes_read < size:
            abs_offset = offset + bytes_read
            block_index = abs_offset // self.header.block_size
            block_offset = abs_offset % self.header.block_size
            bytes_to_read = min(size - bytes_read, self.header.block_size - block_offset)
            
            data_offset = self.header.get_block_offset(block_index)
            
            if data_offset == 0:
                # Unallocated block - return zeros
                pass
            else:
                try:
                    with open(self.path, 'rb') as f:
                        f.seek(data_offset + block_offset)
                        block_data = f.read(bytes_to_read)
                        result[bytes_read:bytes_read + len(block_data)] = block_data
                except Exception:
                    pass
            
            bytes_read += bytes_to_read
        
        return bytes(result)
    
    def write(self, offset: int, data: bytes):
        """Write data to the GSDX image, allocating blocks as needed."""
        if not self.header:
            # Fallback: write to raw file
            with open(self.path, 'r+b') as f:
                f.seek(offset)
                f.write(data)
            return
        
        bytes_written = 0
        data_offset_pos = self._get_data_end()
        
        while bytes_written < len(data):
            abs_offset = offset + bytes_written
            block_index = abs_offset // self.header.block_size
            block_offset = abs_offset % self.header.block_size
            bytes_to_write = min(len(data) - bytes_written, self.header.block_size - block_offset)
            
            file_block_offset = self.header.get_block_offset(block_index)
            
            if file_block_offset == 0:
                # Need to allocate this block
                file_block_offset = data_offset_pos
                self.header.allocate_block(block_index, file_block_offset)
                data_offset_pos += self.header.block_size
                self._update_header()
            
            # Write the data
            with open(self.path, 'r+b') as f:
                f.seek(file_block_offset + block_offset)
                f.write(data[bytes_written:bytes_written + bytes_to_write])
            
            bytes_written += bytes_to_write
        
        # Update used size
        self.header.used_data_size = max(
            self.header.used_data_size,
            offset + len(data)
        )
        self._update_header()
    
    def _get_data_end(self) -> int:
        """Get the end of data area (for appending new blocks)."""
        if not self.header:
            return 0
        
        # Find highest allocated block offset
        max_offset = 0
        for block_off in self.header.bat:
            if block_off > max_offset:
                max_offset = block_off
        
        if max_offset == 0:
            return self.header.HEADER_SIZE + (len(self.header.bat) * 8) + 100
        return max_offset + self.header.block_size
    
    def _update_header(self):
        """Write updated header back to file."""
        if not self.header:
            return
        
        # Update allocated size
        if self.path.exists():
            self.header.allocated_size = self.path.stat().st_size
        
        with open(self.path, 'r+b') as f:
            f.seek(0)
            f.write(self.header.to_bytes())
    
    def get_raw_data(self, max_size: int = 512 * 1024 * 1024) -> bytes:
        """Get contiguous raw data (for VM compatibility).
        
        Expands sparse blocks into a flat byte array.
        """
        if not self.header or not self.path.exists():
            # Try reading as raw file
            if self.path.exists():
                with open(self.path, 'rb') as f:
                    return f.read(max_size)
            return b'\x00' * min(4096, max_size)
        
        result = bytearray(min(self.header.used_data_size or self.header.allocated_size or 4096, max_size))
        
        # Read all allocated blocks
        for i, block_off in enumerate(self.header.bat):
            if block_off == 0:
                continue
            
            block_start = i * self.header.block_size
            if block_start >= len(result):
                continue
            
            bytes_to_read = min(self.header.block_size, len(result) - block_start)
            
            try:
                with open(self.path, 'rb') as f:
                    f.seek(block_off)
                    block_data = f.read(bytes_to_read)
                    result[block_start:block_start + len(block_data)] = block_data
            except Exception:
                pass
        
        return bytes(result)
    
    def set_raw_data(self, data: bytes):
        """Write raw data, creating blocks as needed."""
        self.write(0, data)
    
    def close(self):
        """Close and finalize the GSDX file."""
        if self.header:
            self._update_header()


def detect_disk_format(path: Path) -> str:
    """Detect whether a disk file is GSDX, GSD (raw), or unknown."""
    if not path.exists():
        return "unknown"
    
    # Check for GSDX magic
    try:
        with open(path, 'rb') as f:
            magic = f.read(4)
            if magic == b'GSDX':
                return "gsdx"
    except Exception:
        pass
    
    # Check extension
    suffix = path.suffix.lower()
    if suffix == '.gsdx':
        return "gsdx"
    elif suffix == '.gsd':
        return "gsd"
    
    return "raw"


# ============================================================================
# Opcode Definitions (Galactose VM 13-opcode RISC instruction set)
# ============================================================================

class Opcode(Enum):
    PUSH = 0x01
    POP = 0x02
    ADD = 0x03
    JMP = 0x04
    JZ = 0x05
    HALT = 0x06
    SUB = 0x07
    LOAD = 0x08
    STORE = 0x09
    WRITE_DISK = 0x0A
    READ_DISK = 0x0B
    RAM_STORE = 0x0C # New: specifically for writing to system RAM
    PUSH_REG = 0x0D  # Push a register's value onto the stack

class BytecodeCompiler:
    def __init__(self):
        self.labels = {}
        self.label_refs = []

    def _parse_val(self, val_str):
        val_str = val_str.lower().strip()
        if val_str.startswith('r'):
            return int(val_str.replace('r', ''))
        return int(val_str)

    def compile(self, assembly_text):
        lines = assembly_text.strip().split('\n')
        instruction_lines = []
        current_offset = 0
        
        # Pass 1: Collect Labels
        for line in lines:
            line = line.split(';')[0].strip()
            if not line: continue
            if line.endswith(':'):
                self.labels[line[:-1].strip()] = current_offset
            else:
                instruction_lines.append(line)
                parts = line.split()
                op = parts[0].upper()
                if op in ['PUSH', 'JMP', 'JZ', 'LOAD']: current_offset += 5
                elif op in ['STORE', 'RAM_STORE']: current_offset += 6
                elif op in ['POP', 'PUSH_REG']: current_offset += 2
                else: current_offset += 1 

        bytecode = bytearray()
        self.label_refs = []
        
        # Pass 2: Generate Bytecode
        for line in instruction_lines:
            parts = line.split()
            op = parts[0].upper()
            try:
                if op == 'PUSH':
                    bytecode.append(Opcode.PUSH.value)
                    bytecode.extend(struct.pack('<i', self._parse_val(parts[1])))
                elif op == 'POP':
                    bytecode.append(Opcode.POP.value)
                    bytecode.append(self._parse_val(parts[1]))
                elif op in ['ADD', 'SUB', 'HALT', 'WRITE_DISK', 'READ_DISK']:
                    bytecode.append(Opcode[op].value)
                elif op == 'JMP':
                    bytecode.append(Opcode.JMP.value)
                    if parts[1].isdigit():
                        bytecode.extend(struct.pack('<I', int(parts[1])))
                    else:
                        self.label_refs.append((len(bytecode), parts[1]))
                        bytecode.extend(b'\x00\x00\x00\x00')
                elif op == 'JZ':
                    bytecode.append(Opcode.JZ.value)
                    if parts[1].isdigit():
                        bytecode.extend(struct.pack('<I', int(parts[1])))
                    else:
                        self.label_refs.append((len(bytecode), parts[1]))
                        bytecode.extend(b'\x00\x00\x00\x00')
                elif op == 'LOAD':
                    bytecode.append(Opcode.LOAD.value)
                    bytecode.append(self._parse_val(parts[1]))
                    bytecode.extend(struct.pack('<I', self._parse_val(parts[2])))
                elif op == 'STORE':
                    bytecode.append(Opcode.STORE.value)
                    bytecode.extend(struct.pack('<I', self._parse_val(parts[1])))
                    bytecode.append(self._parse_val(parts[2]))
                elif op == 'RAM_STORE':
                    bytecode.append(Opcode.RAM_STORE.value)
                    bytecode.extend(struct.pack('<I', self._parse_val(parts[1])))
                    bytecode.append(self._parse_val(parts[2]))
                elif op == 'PUSH_REG':
                    bytecode.append(Opcode.PUSH_REG.value)
                    bytecode.append(self._parse_val(parts[1]))
            except Exception as e:
                print(f"Error parsing line '{line}': {e}")
                sys.exit(1)

        for offset, name in self.label_refs:
            if name in self.labels:
                struct.pack_into('<I', bytecode, offset, self.labels[name])
            else:
                print(f"Undefined label: {name}")
                sys.exit(1)
        return bytes(bytecode)

class BytecodeVM:
    def __init__(self, memory_size_mb=512, vram_size=640*480*3,
                 disk_path=None, bios_bytecode=None, image_path=None):
        self.memory = bytearray(memory_size_mb * 1024 * 1024)
        self.vram = bytearray(vram_size)
        self.stack = []
        self.registers = [0] * 256
        self.pc = 0
        self.bytecode = bytearray()
        self.running = False
        self.root = None
        self.canvas = None
        self.width = 640
        self.height = 480

        # ── Disk 0: internal persistent storage (data.gsd OR data.gsdx) ──────
        if disk_path:
            self.disk_path = Path(disk_path)
        else:
            default_dir = Path.home() / ".galactose"
            default_dir.mkdir(exist_ok=True)
            # Prefer GSDX format if available, fall back to GSD
            gsdx_path = default_dir / "data.gsdx"
            gsd_path = default_dir / "data.gsd"
            if gsdx_path.exists():
                self.disk_path = gsdx_path
            elif gsd_path.exists():
                self.disk_path = gsd_path
            else:
                self.disk_path = gsdx_path  # Default to GSDX for new installations
        
        # Initialize GSDX handler for internal disk
        self._disk_gsdx: Optional[GSDXDiskImage] = None
        self._is_gsdx_disk = False
        
        if not self.disk_path.exists():
            # Auto-detect format from extension
            if self.disk_path.suffix.lower() == '.gsdx':
                self._create_blank_gsdx(self.disk_path, 512)
            else:
                self.create_blank_disk(self.disk_path, 512)
        
        # Check if existing disk is GSDX format
        fmt = detect_disk_format(self.disk_path)
        if fmt == "gsdx":
            self._is_gsdx_disk = True
            self._disk_gsdx = GSDXDiskImage(self.disk_path)
            print(f"[GSDX] Internal disk using dynamic format: {self.disk_path}")

        # ── Disk 1: removable image (flash drive / CD-ROM equivalent) ───────
        self.image_path = Path(image_path) if image_path else None
        self._image_gsdx: Optional[GSDXDiskImage] = None
        self._is_gsdx_image = False
        
        # Initialize GSDX handler for removable image if provided
        if self.image_path and self.image_path.exists():
            img_fmt = detect_disk_format(self.image_path)
            if img_fmt == "gsdx":
                self._is_gsdx_image = True
                self._image_gsdx = GSDXDiskImage(self.image_path)
                print(f"[GSDX] Removable image using dynamic format: {self.image_path}")

        # ── BIOS bytecode (prepended before main program) ───────────────────
        # Stored separately; spliced into self.bytecode in execute().
        self.bios_bytecode = bios_bytecode or bytearray()

        # ── Register map ────────────────────────────────────────────────────
        # Audio
        self.freq_reg   = 200   # (w) tone frequency Hz → triggers playback
        self.dur_reg    = 201   # (w) tone duration ms
        # Disk select
        self.disk_sel_reg = 202  # (r/w) 0 = internal data.gsd, 1 = --image
        self.image_id_reg = 203  # (r)  1 when --image is mounted, else 0
        # Network control (210-219) — all side-effects triggered by POP
        self.NET_CMD        = 210  # (w) 1=TCP connect, 2=send, 3=recv,
                                   #     4=close, 5=UDP bind, 6=sendto, 7=recvfrom
        self.NET_ADDR_PTR   = 211  # (w) RAM address of null-terminated host string
        self.NET_PORT       = 212  # (w) remote port (connect/sendto) or local port (bind)
        self.NET_BUF_PTR    = 213  # (w) RAM address of send/recv data buffer
        self.NET_BUF_LEN    = 214  # (w) byte length of buffer
        self.NET_SOCKET_ID  = 215  # (r/w) socket handle; set by VM after connect/bind
        self.NET_RESULT     = 216  # (r) bytes transferred, or -1 on error
        self.NET_STATUS     = 217  # (r) 0=idle, 1=busy, 2=data_ready, 3=error
        self.NET_PROTO      = 218  # (w) 0=TCP (default), 1=UDP
        self.NET_REMOTE_PORT= 219  # (r) source port populated after UDP recvfrom
        # Input / timing
        self.input_reg  = 254   # (r) last key code
        self.timing_reg = 255   # (w) sleep ms

        # ── Socket table: socket_id (int) → socket object ───────────────────
        self._sockets = {}          # id → socket.socket
        self._next_socket_id = 1
        self._net_lock = threading.Lock()

        # ── Initialise image_id register ────────────────────────────────────
        self.registers[self.image_id_reg] = 1 if self.image_path else 0
        
    # ── Disk helpers ────────────────────────────────────────────────────────

    def _active_disk(self):
        """Return the path of the currently selected disk (register 202)."""
        sel = self.registers[self.disk_sel_reg]
        if sel == 1 and self.image_path:
            return self.image_path
        return self.disk_path
    
    def _active_gsdx(self) -> Optional[GSDXDiskImage]:
        """Return the GSDX handler for the currently selected disk, or None."""
        sel = self.registers[self.disk_sel_reg]
        if sel == 1:
            return self._image_gsdx
        return self._disk_gsdx
    
    def _is_active_gsdx(self) -> bool:
        """Check if the currently selected disk is using GSDX format."""
        sel = self.registers[self.disk_sel_reg]
        if sel == 1:
            return self._is_gsdx_image
        return self._is_gsdx_disk

    # ── Network execution (runs in a daemon thread, updates registers) ───────

    def _net_exec(self):
        """Execute the network command stored in NET_CMD (register 210).

        All inputs are read from registers before the thread starts so there
        is no race with subsequent POP side-effects.  Results are written back
        to NET_RESULT, NET_STATUS, NET_SOCKET_ID, and NET_REMOTE_PORT.
        """
        cmd        = self.registers[self.NET_CMD]
        addr_ptr   = self.registers[self.NET_ADDR_PTR]
        port       = self.registers[self.NET_PORT]
        buf_ptr    = self.registers[self.NET_BUF_PTR]
        buf_len    = self.registers[self.NET_BUF_LEN]
        sock_id    = self.registers[self.NET_SOCKET_ID]
        proto      = self.registers[self.NET_PROTO]   # 0=TCP, 1=UDP

        self.registers[self.NET_STATUS] = 1  # busy

        try:
            # ── Helper: read null-terminated string from RAM ─────────────────
            def _read_cstr(base):
                end = base
                while end < len(self.memory) and self.memory[end] != 0:
                    end += 1
                return self.memory[base:end].decode("utf-8", errors="replace")

            # ── Helper: get existing socket by id ───────────────────────────
            def _get_sock(sid):
                with self._net_lock:
                    s = self._sockets.get(sid)
                if s is None:
                    raise KeyError(f"Unknown socket id {sid}")
                return s

            # ── Helper: allocate a new socket id ────────────────────────────
            def _register_sock(s):
                with self._net_lock:
                    sid = self._next_socket_id
                    self._next_socket_id += 1
                    self._sockets[sid] = s
                return sid

            # ── 1: TCP connect ───────────────────────────────────────────────
            if cmd == 1:
                host = _read_cstr(addr_ptr)
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(10)
                s.connect((host, port))
                sid = _register_sock(s)
                self.registers[self.NET_SOCKET_ID] = sid
                self.registers[self.NET_RESULT]    = sid
                self.registers[self.NET_STATUS]    = 2  # data_ready / connected

            # ── 2: send (TCP or UDP connected) ──────────────────────────────
            elif cmd == 2:
                s    = _get_sock(sock_id)
                data = bytes(self.memory[buf_ptr : buf_ptr + buf_len])
                sent = s.sendall(data) or buf_len   # sendall returns None on success
                self.registers[self.NET_RESULT] = buf_len
                self.registers[self.NET_STATUS] = 0  # idle

            # ── 3: recv (TCP) ────────────────────────────────────────────────
            elif cmd == 3:
                s    = _get_sock(sock_id)
                data = s.recv(buf_len)
                n    = len(data)
                self.memory[buf_ptr : buf_ptr + n] = data
                self.registers[self.NET_RESULT] = n
                self.registers[self.NET_STATUS] = 2 if n > 0 else 0

            # ── 4: close ─────────────────────────────────────────────────────
            elif cmd == 4:
                with self._net_lock:
                    s = self._sockets.pop(sock_id, None)
                if s:
                    try: s.close()
                    except Exception: pass
                self.registers[self.NET_RESULT] = 0
                self.registers[self.NET_STATUS] = 0

            # ── 5: UDP bind (create a UDP socket bound to local port) ────────
            elif cmd == 5:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(5)
                s.bind(("", port))
                sid = _register_sock(s)
                self.registers[self.NET_SOCKET_ID] = sid
                self.registers[self.NET_RESULT]    = sid
                self.registers[self.NET_STATUS]    = 0

            # ── 6: UDP sendto ────────────────────────────────────────────────
            elif cmd == 6:
                s    = _get_sock(sock_id)
                host = _read_cstr(addr_ptr)
                data = bytes(self.memory[buf_ptr : buf_ptr + buf_len])
                s.sendto(data, (host, port))
                self.registers[self.NET_RESULT] = buf_len
                self.registers[self.NET_STATUS] = 0

            # ── 7: UDP recvfrom ──────────────────────────────────────────────
            elif cmd == 7:
                s = _get_sock(sock_id)
                data, (remote_host, remote_port) = s.recvfrom(buf_len)
                n = len(data)
                self.memory[buf_ptr : buf_ptr + n] = data
                self.registers[self.NET_RESULT]      = n
                self.registers[self.NET_REMOTE_PORT] = remote_port
                self.registers[self.NET_STATUS]      = 2 if n > 0 else 0

            else:
                self.registers[self.NET_STATUS] = 3  # unknown command → error
                self.registers[self.NET_RESULT] = -1

        except Exception as e:
            print(f"Network error (cmd={cmd}): {e}")
            self.registers[self.NET_STATUS] = 3   # error
            self.registers[self.NET_RESULT] = -1

    @staticmethod
    def create_blank_disk(path, size_mb):
        """Create a blank legacy .gsd disk image (fixed-size raw format)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        chunk_size = 1024 * 1024
        total_bytes = size_mb * 1024 * 1024
        zeros = b'\x00' * chunk_size
        
        print(f"Allocating disk ({size_mb}MB) at {path}...")
        with open(path, "wb") as f:
            for _ in range(size_mb):
                f.write(zeros)
            f.flush()
            os.fsync(f.fileno())
        print("Disk allocation complete.")
    
    @staticmethod
    def create_blank_gsdx(path, max_size_mb=512):
        """Create a blank .gsdx disk image (dynamic sparse format).
        
        Unlike create_blank_disk(), this does NOT pre-allocate space.
        The image grows dynamically as data is written.
        
        Args:
            path: Output file path (should end in .gsdx)
            max_size_mb: Maximum theoretical size in MB (0 = unlimited)
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        gsdx = GSDXDiskImage(path, create_new=True, max_size_mb=max_size_mb)
        gsdx.close()
        print(f"[GSDX] Dynamic disk created: {path}")

    def init_display(self):
        self.root = tk.Tk()
        self.root.title("Galactose VM")
        self.canvas = tk.Canvas(self.root, width=self.width, height=self.height, bg="black", highlightthickness=0)
        self.canvas.pack()
        # PhotoImage used for bulk pixel rendering — far cheaper than per-pixel create_rectangle calls
        self.photo = tk.PhotoImage(width=self.width, height=self.height)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.root.bind("<KeyPress>", self._on_key_press)
        self.root.bind("<KeyRelease>", self._on_key_release)
        self.update_display()

    def _on_key_press(self, event):
        mapping = {"Up": 1, "Down": 2, "Left": 3, "Right": 4}
        self.registers[self.input_reg] = mapping.get(event.keysym, ord(event.char) if len(event.char) == 1 else 0)

    def _on_key_release(self, event):
        self.registers[self.input_reg] = 0

    def update_display(self):
        if not self.root: return
        mode = "text"
        cursor_x, cursor_y = 10, 20
        i = 0
        vram_view = bytes(self.vram)

        # Build a flat list of "#rrggbb" strings for every screen pixel,
        # then push the entire frame to the PhotoImage in one call.
        # This avoids creating tens of thousands of canvas objects per frame.
        pixel_colors = ["#000000"] * (self.width * self.height)
        text_items = []  # (x, y, char) tuples drawn after the image
        pixel_index = 0

        while i < len(vram_view):
            byte = vram_view[i]
            if byte == 0xDF:
                mode = "pixel"
                pixel_index = 0
                i += 1
                continue
            elif byte == 0xDC:
                mode = "text"
                i += 1
                continue

            if mode == "text" and byte != 0:
                text_items.append((cursor_x, cursor_y, chr(byte)))
                cursor_x += 8
                if cursor_x > self.width - 20:
                    cursor_x = 10
                    cursor_y += 18
                i += 1
            elif mode == "pixel":
                if i + 2 < len(vram_view):
                    r, g, b = vram_view[i], vram_view[i + 1], vram_view[i + 2]
                else:
                    r, g, b = 255, 255, 255
                if pixel_index < len(pixel_colors):
                    pixel_colors[pixel_index] = f"#{r:02x}{g:02x}{b:02x}"
                pixel_index += 1
                i += 3
            else:
                i += 1

        # Push entire pixel frame in one PhotoImage call
        row_strings = []
        for y in range(self.height):
            row = pixel_colors[y * self.width : (y + 1) * self.width]
            row_strings.append("{" + " ".join(row) + "}")
        self.photo.put(" ".join(row_strings), to=(0, 0, self.width, self.height))

        # Redraw text overlay on top of the image
        self.canvas.delete("text_overlay")
        for (tx, ty, ch) in text_items:
            self.canvas.create_text(tx, ty, text=ch, fill="white", font=("Monaco", 12), anchor="nw", tags="text_overlay")

        self.root.after(33, self.update_display)

    def _run_vm_loop(self):
        self.pc = 0
        while self.running and self.pc < len(self.bytecode):
            op = self.bytecode[self.pc]
            self.pc += 1
            self._dispatch(op)
        self.running = False

    def execute(self):
        # Splice BIOS bytecode (if any) before the main program bytecode.
        # The BIOS runs from PC=0; the main program immediately follows so
        # a BIOS that doesn't HALT will fall through into it naturally.
        if self.bios_bytecode:
            self.bytecode = bytearray(self.bios_bytecode) + bytearray(self.bytecode)
            print(f"BIOS loaded: {len(self.bios_bytecode)} bytes prepended to program.")
        self.running = True
        vm_thread = threading.Thread(target=self._run_vm_loop, daemon=True)
        vm_thread.start()
        if self.root:
            self.root.mainloop()

    def _dispatch(self, op):
        try:
            if op == Opcode.PUSH.value:
                v = struct.unpack_from('<i', self.bytecode, self.pc)[0]
                self.pc += 4; self.stack.append(v)
            elif op == Opcode.POP.value:
                r = self.bytecode[self.pc]; self.pc += 1
                v = self.stack.pop() if self.stack else 0
                self.registers[r] = v
                if r == self.freq_reg:
                    freq, dur = v, self.registers[self.dur_reg]
                    if dur <= 0: dur = 100
                    threading.Thread(target=play_tone, args=(freq, dur), daemon=True).start()
                elif r == self.timing_reg:
                    if v > 0: time.sleep(v / 1000.0)
                elif r == self.NET_CMD and v != 0:
                    # Writing a non-zero command to NET_CMD fires the network
                    # operation asynchronously so the VM loop is never blocked.
                    threading.Thread(target=self._net_exec, daemon=True).start()
            elif op == Opcode.ADD.value:
                if len(self.stack) >= 2: self.stack.append(self.stack.pop() + self.stack.pop())
            elif op == Opcode.SUB.value:
                if len(self.stack) >= 2:
                    b, a = self.stack.pop(), self.stack.pop()
                    self.stack.append(a - b)
            elif op == Opcode.STORE.value:
                addr = struct.unpack_from('<I', self.bytecode, self.pc)[0]; self.pc += 4
                r = self.bytecode[self.pc]; self.pc += 1
                self.vram[addr % len(self.vram)] = self.registers[r] & 0xFF
            elif op == Opcode.RAM_STORE.value:
                addr = struct.unpack_from('<I', self.bytecode, self.pc)[0]; self.pc += 4
                r = self.bytecode[self.pc]; self.pc += 1
                # Write register value to System Memory (not VRAM)
                struct.pack_into('<i', self.memory, addr % len(self.memory), self.registers[r])
            elif op == Opcode.LOAD.value:
                r = self.bytecode[self.pc]; self.pc += 1
                addr = struct.unpack_from('<I', self.bytecode, self.pc)[0]; self.pc += 4
                val = struct.unpack_from('<i', self.memory, addr % len(self.memory))[0]
                self.registers[r] = val
            elif op == Opcode.JMP.value:
                self.pc = struct.unpack_from('<I', self.bytecode, self.pc)[0]
            elif op == Opcode.JZ.value:
                addr = struct.unpack_from('<I', self.bytecode, self.pc)[0]; self.pc += 4
                if not self.stack or self.stack.pop() == 0: self.pc = addr
            elif op == Opcode.WRITE_DISK.value:
                length = self.stack.pop()
                addr   = self.stack.pop()
                data   = self.memory[addr : addr + length]
                target = self._active_disk()
                gsdx_handler = self._active_gsdx()
                
                if gsdx_handler and self._is_active_gsdx():
                    # Use GSDX sparse write (allocates blocks on-demand)
                    print(f"DEBUG: Writing {length} bytes to GSDX disk '{target.name}'...")
                    gsdx_handler.set_raw_data(data)
                else:
                    # Legacy raw write
                    print(f"DEBUG: Writing {length} bytes from RAM[{addr}] to disk '{target.name}'...")
                    with open(target, "r+b") as f:
                        f.seek(0)
                        f.write(data)
                        f.flush()
                        os.fsync(f.fileno())
            elif op == Opcode.READ_DISK.value:
                length = self.stack.pop()
                addr   = self.stack.pop()
                target = self._active_disk()
                gsdx_handler = self._active_gsdx()
                
                if gsdx_handler and self._is_active_gsdx():
                    # Use GSDX sparse read (unallocated blocks return zeros)
                    data = gsdx_handler.get_raw_data(length)
                    self.memory[addr : addr + len(data)] = data
                elif target.exists():
                    with open(target, "rb") as f:
                        f.seek(0)
                        data = f.read(length)
                        self.memory[addr : addr + len(data)] = data
            elif op == Opcode.HALT.value:
                self.running = False
            elif op == Opcode.PUSH_REG.value:
                r = self.bytecode[self.pc]; self.pc += 1
                self.stack.append(self.registers[r])
        except Exception as e:
            print(f"VM Execution Error at PC {self.pc-1}: {e}")
            self.running = False

def main():
    parser = argparse.ArgumentParser(
        description="Galactose VM - RISC Bytecode Virtual Machine with GSDX Support",
        epilog="Examples:\n"
                "  python3 galactose.py --compile prog.asm -o prog.gcb\n"
                "  python3 galactose.py --execute prog.gcb\n"
                "  python3 galactose.py --new-gsdx --disk-size 1024 -o data.gsdx\n"
                "  python3 galactose.py --new-disk --disk-size 512 -o data.gsd\n"
                "  python3 galactose.py --execute prog.gcb --image disk.gsdx\n",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--compile', help="Source file to compile")
    parser.add_argument('--output', '-o', help="Output path (bytecode or disk)")
    parser.add_argument('--execute', help="Bytecode file to execute")
    parser.add_argument('--memory', type=int, default=512, help="Memory size in MB")
    parser.add_argument('--disk', help=(
        "Path to custom disk file (internal, disk 0). "
        "Supports both legacy .gsd and new .gsdx formats. "
        "Auto-detected from file content or extension."
    ))
    parser.add_argument('--new-disk', action='store_true', 
                        help="Create a new blank legacy .gsd disk (fixed-size, pre-allocated)")
    parser.add_argument('--new-gsdx', action='store_true',
                        help="Create a new blank .gsdx disk (dynamic/sparse, grows on demand)")
    parser.add_argument('--disk-size', type=int, default=512, help=(
        "Size for new disk in MB. "
        "For --new-disk: actual pre-allocated size. "
        "For --new-gsdx: maximum theoretical size (0 = unlimited)."
    ))
    parser.add_argument('--bios', help="BIOS bytecode (.gcb) to run before the main program")
    parser.add_argument('--image', help=(
        "Removable image to mount as disk 1 (flash drive / CD-ROM). "
        "Supports both .gsd and .gsdx formats (auto-detected). "
        "Select it at runtime by writing 1 to register 202 (disk_sel). "
        "Register 203 (image_id) reads 1 when an image is mounted, else 0."
    ))

    args = parser.parse_args()

    # ── Handle --new-gsdx (create dynamic sparse disk) ──────────────────────
    if args.new_gsdx:
        dest = args.output if args.output else (Path.home() / ".galactose" / "data.gsdx")
        # Ensure .gsdx extension
        dest = Path(dest)
        if dest.suffix.lower() != '.gsdx':
            dest = dest.with_suffix('.gsdx')
        BytecodeVM.create_blank_gsdx(dest, args.disk_size)
        return

    # ── Handle --new-disk (create legacy fixed-size disk) ───────────────────
    if args.new_disk:
        dest = args.output if args.output else (Path.home() / ".galactose" / "data.gsd")
        BytecodeVM.create_blank_disk(dest, args.disk_size)
        return

    if args.compile:
        out_path = args.output if args.output else "program.gcb"
        with open(args.compile, 'r') as f:
            bc = BytecodeCompiler().compile(f.read())
        with open(out_path, 'wb') as f:
            f.write(bc)
        print(f"Successfully compiled to {out_path}")

    if args.execute:
        # Load optional BIOS bytecode
        bios_bc = None
        if args.bios:
            with open(args.bios, 'rb') as f:
                bios_bc = bytearray(f.read())
            print(f"BIOS: {args.bios} ({len(bios_bc)} bytes)")

        # Validate optional removable image (supports both .gsd and .gsdx)
        image_path = None
        if args.image:
            image_path = Path(args.image)
            if not image_path.exists():
                print(f"Warning: --image path '{image_path}' does not exist. "
                      "Register 203 will read 0 (no image mounted).")
                image_path = None
            else:
                fmt = detect_disk_format(image_path)
                fmt_label = "GSDX (dynamic)" if fmt == "gsdx" else "GSD (legacy)"
                print(f"Image mounted: {image_path} (disk 1, format: {fmt_label}, register 202 = 1 to select)")

        vm = BytecodeVM(
            memory_size_mb=args.memory,
            disk_path=args.disk,
            bios_bytecode=bios_bc,
            image_path=image_path,
        )
        with open(args.execute, 'rb') as f:
            vm.bytecode = bytearray(f.read())
        vm.init_display()
        vm.execute()

if __name__ == '__main__':
    main()
