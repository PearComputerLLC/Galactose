Galactose

A Lightweight, Turing-Complete 13-Opcode RISC ISA

Galactose is a minimalist 13-opcode RISC Instruction Set Architecture (ISA) that is:

Turing-complete — capable of scaling to any computational task.
Learnable in a day — simple and consistent, perfect for enthusiasts and professionals alike.
Portable and adaptable — designed to run anywhere, from embedded systems to large-scale systems.
Tiny and self-contained — just a 26KB Python file powering the VM, compiler, and test environment.

Unlike typical RISC ISAs that demand specialization and complexity, Galactose thrives on extreme simplicity while maintaining full computational power.

---

Why Galactose?

Minimal yet Powerful
With only 13 opcodes, the ISA is small enough to learn in a day.
Despite its size, Galactose is Turing-complete and can handle any task.
Portable Anywhere
Ultra-compact 26KB Python implementation.
Easily integrated into projects of any scale, from microcontrollers to servers.
One-Stop Toolchain
galactose.py — the official VM, compiler, and test environment for the standard Galactose variant.
galactose_decompiler.py — decompiles .gcb binaries back into Galactose .gasm assembly.
Flexible Storage Formats
.gsd — raw disk format for direct, full-image access.
.gsdx — dynamic disk format that only allocates space for accessed data.

---

Features

13-opcode RISC ISA
Fully Turing-complete
26KB Python implementation
VM, compiler, and test suite included
Portable and embeddable
Supports .gsd and .gsdx virtual disk formats

---

Getting Started

Clone the repository and run the Galactose VM:

git clone https://github.com/PearComputerLLC/Galactose.git
cd galactose
python galactose.py --compile your_program.gasm -o your_program.gcb

To decompile binaries:

Run python galactose_decompiler.py and use the GUI to decompile

---

Example Usage

# Compile and run Galactose assembly
python galactose.py --compile examples/hello.gasm -o examples/hello.gcb

# Decompile binary back to assembly
python galactose_decompiler.py and use the GUI on examples/hello.gcb

---

Disk Image Options

Raw Disk (.gsd)
Straightforward sector-by-sector image.
Perfect for low-level or full-disk use cases.
Dynamic Disk (.gsdx)
Only stores sections of the disk actually accessed.
Ideal for lightweight or file-specific operations.

---

License

Galactose is released under the MIT License. Contributions and forks are welcome!

---

Summary

Galactose is small, fast, flexible, and Turing-complete — whether you are learning low-level concepts, embedding a micro-VM, or prototyping new ideas, Galactose can adapt to your tasks with simplicity and power.

Explore the code. Learn the ISA. Build something new with Galactose today!
