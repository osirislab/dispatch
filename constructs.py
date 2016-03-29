import subprocess
import logging
import capstone
from enums import *

class Function(object):
    NORMAL_FUNC = 0
    DYNAMIC_FUNC = 1

    def __init__(self, address, size, name, executable, type=NORMAL_FUNC):
        self.address = address
        self.size = size
        self.name = name
        self.type = type
        self._executable = executable

        # BELOW: Helpers used to explore the binary.
        # NOTE: These should *not* be directly modified at this time.
        # Instead, executable.replace_instruction should be used.
        self.instructions = [] # Sequential list of instructions
        self.bbs = [] # Sequential list of basic blocks. BB instructions are auto-populated from our instructions
    
    def __repr__(self):
        return '<Function \'{}\' at {}>'.format(self.name, hex(self.address))
    
    def contains_address(self, address):
        return self.address <= address < self.address + self.size

    def iter_bbs(self):
        for bb in self.bbs:
            yield bb

    def print_disassembly(self):
        for i in self.instructions:
            print hex(i.address) + ' ' + str(i)

    def demangle(self):
        if self.name.startswith('_Z'):
            p = subprocess.Popen(['c++filt', '-n', self.name], stdout=subprocess.PIPE)
            demangled, _ = p.communicate()
            return demangled.replace('\n','')
        elif self.name.startswith('@'):
            # TODO: MSVC demangling (look at wine debugger source)
            return self.name
        else:
            logging.debug('Call to demangle with a non-reserved function name')



class BasicBlock(object):
    def __init__(self, parent_func, address, size):
        self.parent = parent_func
        self.address = address
        self.size = size
        self.offset = self.parent.address - self.address
        self.instructions = [i for i in self.parent.instructions if self.address <= i.address < self.address + self.size]
    
    def __repr__(self):
        return '<Basic block at {}>'.format(hex(self.address))
    
    def print_disassembly(self):
        for i in self.instructions:
            print hex(i.address) + ' ' + str(i)

class Instruction(object):
    GRP_CALL = 0
    GRP_JUMP = 1

    def __init__(self, address, size, raw, mnemonic, operands, groups, backend_instruction, executable):
        self.address = int(address)
        self.size = int(size)
        self.raw = raw
        self.mnemonic = mnemonic
        self.operands = operands
        self.groups = groups
        self._backend_instruction = backend_instruction
        self._executable = executable

        self.comment = ''

    def __repr__(self):
        return '<Instruction at {}>'.format(hex(self.address))

    def __str__(self):
        s = self.mnemonic + ' ' + self.nice_op_str()
        if self.comment:
            s += '; "{}"'.format(self.comment)
        if self.address in self._executable.xrefs:
            s += '; XREF={}'.format(', '.join(hex(a)[:-1] for a in self._executable.xrefs[self.address]))
            # TODO: Print nice function relative offsets if the xref is in a function

        return s

    def is_call(self):
        return Instruction.GRP_CALL in self.groups

    def is_jump(self):
        return Instruction.GRP_JUMP in self.groups

    def nice_op_str(self):
        '''
        Returns the operand string "nicely formatted." I.e. replaces addresses with function names (and function
        relative offsets) if appropriate.
        :return: The nicely formatted operand string
        '''
        op_strings = [str(op) for op in self.operands]

        # If this is an immediate call or jump, try to put a name to where we're calling/jumping to
        if self.is_call() or self.is_jump():
            # jump/call destination will always be the last operand (even with conditional ARM branch instructions)
            operand = self.operands[-1]
            if operand.imm in self._executable.functions:
                op_strings[-1] = self._executable.functions[operand.imm].name
            elif self._executable.vaddr_is_executable(operand.imm):
                func_addrs = self._executable.functions.keys()
                func_addrs.sort(reverse=True)
                if func_addrs:
                    for func_addr in func_addrs:
                        if func_addr < operand.imm:
                            break
                    diff = operand.imm - func_addr
                    op_strings[-1] = self._executable.functions[func_addr].name+'+'+hex(diff)
        else:
            for i, operand in enumerate(self.operands):
                if operand.type == Operand.IMM and operand.imm in self._executable.strings:
                    referenced_string = self._executable.strings[operand.imm]
                    op_strings[i] = referenced_string.short_name
                    self.comment = referenced_string.string

        return ', '.join(op_strings)

class Operand(object):
    IMM = 0
    FP = 1
    REG = 2
    MEM = 3

    def __init__(self, type, instruction, **kwargs):
        self.type = type
        self._instruction = instruction
        if self.type == Operand.IMM:
            self.imm = int(kwargs.get('imm'))
        elif self.type == Operand.FP:
            self.fp = float(kwargs.get('fp'))
        elif self.type == Operand.REG:
            self.reg = kwargs.get('reg')
        elif self.type == Operand.MEM:
            self.base = kwargs.get('base')
            self.index = kwargs.get('index')
            self.scale = int(kwargs.get('scale', 1))
            self.disp = int(kwargs.get('disp', 0))
        else:
            raise ValueError('Type is not one of Operand.{IMM,FP,REG,MEM}')

    def _get_simplified(self):
        # Auto-simplify ip-relative operands to their actual address
        if self.type == Operand.MEM and self.base in IP_REGS[self._instruction._executable.architecture] and self.index == 0:
            addr = self._instruction.address + self._instruction.size + self.index * self.scale + self.disp
            return Operand(Operand.MEM, self._instruction, disp=addr)

        return self

    def __str__(self):
        if self.type == Operand.IMM:
            return hex(self.imm)
        elif self.type == Operand.FP:
            return str(self.fp)
        elif self.type == Operand.REG:
            return REGISTER_NAMES[self._instruction._executable.architecture][self.reg]
        elif self.type == Operand.MEM:
            simplified = self._get_simplified()

            s = '['

            show_plus = False
            if simplified.base:
                s += REGISTER_NAMES[simplified._instruction._executable.architecture][simplified.base]
                show_plus = True
            if simplified.index:
                if show_plus:
                    s += ' + '

                s += REGISTER_NAMES[simplified._instruction._executable.architecture][simplified.index]
                if simplified.scale > 1:
                    s += '*'
                    s += str(simplified.scale)

                show_plus = True
            if simplified.disp:
                if show_plus:
                    s += ' + '
                s += hex(simplified.disp)

            s += ']'

            return s

# I'm lazy :P
REGISTER_NAMES = {
    ARCHITECTURE.X86: dict([(v,k[8:].lower()) for k,v in capstone.x86_const.__dict__.iteritems() if k.startswith('X86_REG')]),
    ARCHITECTURE.X86_64: dict([(v,k[8:].lower()) for k,v in capstone.x86_const.__dict__.iteritems() if k.startswith('X86_REG')]),
    ARCHITECTURE.ARM: dict([(v,k[8:].lower()) for k,v in capstone.arm_const.__dict__.iteritems() if k.startswith('ARM_REG')]),
    ARCHITECTURE.ARM_64: dict([(v,k[10:].lower()) for k,v in capstone.arm64_const.__dict__.iteritems() if k.startswith('ARM64_REG')])
}

IP_REGS = {
    ARCHITECTURE.X86: [26, 34, 41],
    ARCHITECTURE.X86_64: [26, 34, 41],
    ARCHITECTURE.ARM: [11],
    ARCHITECTURE.ARM_64: [],
}

SP_REGS = {
    ARCHITECTURE.X86: [30, 44, 47],
    ARCHITECTURE.X86_64: [30, 44, 47],
    ARCHITECTURE.ARM: [12],
    ARCHITECTURE.ARM_64: [4, 5],
}

def operand_from_cs_op(csOp, instruction):
    if csOp.type == capstone.CS_OP_IMM:
        return Operand(Operand.IMM, instruction, imm=csOp.imm)
    elif csOp.type == capstone.CS_OP_FP:
        return Operand(Operand.FP, instruction, fp=csOp.fp)
    elif csOp.type == capstone.CS_OP_REG:
        return Operand(Operand.REG, instruction, reg=csOp.reg)
    elif csOp.type == capstone.CS_OP_MEM:
        return Operand(Operand.MEM, instruction, base=csOp.mem.base, index=csOp.mem.index, scale=csOp.mem.scale, disp=csOp.mem.disp)

def instruction_from_cs_insn(csInsn, executable):
    groups = []
    if capstone.CS_GRP_JUMP in csInsn.groups:
        groups.append(Instruction.GRP_JUMP)
    if capstone.CS_GRP_CALL in csInsn.groups:
        groups.append(Instruction.GRP_CALL)

    instruction = Instruction(csInsn.address, csInsn.size, csInsn.bytes, csInsn.mnemonic, [], groups, csInsn, executable)

    operands = [operand_from_cs_op(op, instruction) for op in csInsn.operands]

    instruction.operands = operands

    return instruction

class String(object):
    def __init__(self, string, vaddr, executable):
        self.string = string
        self.short_name = self.string.replace(' ','')[:8]
        self.vaddr = vaddr
        self._executable = executable

    def __repr__(self):
        return '<String \'{}\' at {}>'.format(self.string, self.vaddr)

    def __str__(self):
        return self.string

class CFGEdge(object):
    # Edge with no special information. Could be from a default fall-through, unconditional jump, etc.
    DEFAULT = 0

    # Edge from a conditional jump. Two of these should be added for each cond. jump, one for the True, and one for False
    COND_JUMP = 1

    # Edge from a switch/jump table. One edge should be added for each entry, and the corresponding key set as the value
    SWITCH = 2

    def __init__(self, src, dst, type, value=None):
        self.src = src
        self.dst = dst
        self.type = type
        self.value = value

    def __eq__(self, other):
        if isinstance(other, CFGEdge) and self.src == other.src and self.dst == other.dst and self.type == other.type:
            return True
        return False

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return '<CFGEdge from {} to {}>'.format(self.src, self.dst)