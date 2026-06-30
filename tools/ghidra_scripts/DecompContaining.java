// Decompile the function CONTAINING each given (possibly mid-function) address, in -noanalysis mode.
// Linearly disassembles a window around the target, walks backward to the function entry (the instruction
// right after a flow terminator: ret / unconditional jmp / gap), creates the function, and decompiles it.
// Headless args: <outDir> <addr1> [addr2 ...]
import ghidra.app.script.GhidraScript;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.symbol.FlowType;
import java.io.File;
import java.io.PrintWriter;

public class DecompContaining extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        File outDir = new File(args[0]);
        outDir.mkdirs();
        DecompInterface ifc = new DecompInterface();
        ifc.toggleCCode(true);
        ifc.openProgram(currentProgram);
        for (int i = 1; i < args.length; i++) {
            Address t = toAddr(args[i]);
            Address start = t.subtract(0x1200);
            AddressSet set = new AddressSet(start, t.add(0x400));
            DisassembleCommand dis = new DisassembleCommand(set, null, true);
            dis.applyTo(currentProgram, monitor);
            Instruction insn = getInstructionContaining(t);
            if (insn == null) { println("NO INSN at " + args[i]); continue; }
            // walk backward to the function entry
            int guard = 0;
            while (guard++ < 4000) {
                Instruction prev = insn.getPrevious();
                if (prev == null) break;
                if (!prev.getMaxAddress().next().equals(insn.getMinAddress())) break;   // gap
                FlowType ft = prev.getFlowType();
                if (ft.isTerminal()) break;                                              // ret
                if (ft.isJump() && !ft.isConditional() && !prev.hasFallthrough()) break; // uncond jmp
                insn = prev;
            }
            Address entry = insn.getMinAddress();
            Function f = getFunctionAt(entry);
            if (f == null) f = getFunctionContaining(t);
            if (f == null) f = createFunction(entry, null);
            if (f == null) { println("NO FUNC for " + args[i] + " (entry " + entry + ")"); continue; }
            DecompileResults res = ifc.decompileFunction(f, 360, monitor);
            String c = (res != null && res.getDecompiledFunction() != null)
                    ? res.getDecompiledFunction().getC() : "// DECOMP FAILED";
            File out = new File(outDir, "fn_at_" + args[i].replace("0x", "") + ".c");
            try (PrintWriter pw = new PrintWriter(out)) { pw.print(c); }
            println("decompiled func @" + f.getEntryPoint() + " containing " + args[i] + " -> " + out.getName());
        }
        ifc.dispose();
    }
}
