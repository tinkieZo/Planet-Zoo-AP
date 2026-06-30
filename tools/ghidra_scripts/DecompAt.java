// Decompile functions at given addresses, creating the function first if analysis hasn't (works with
// -noanalysis: disassemble at the entry, create the function, then decompile). Low-RAM: only touches the
// functions you ask for, so it runs on a huge binary without full auto-analysis.
// Headless args: <outDir> <addr1> [addr2 ...]
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import java.io.File;
import java.io.PrintWriter;

public class DecompAt extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        File outDir = new File(args[0]);
        outDir.mkdirs();
        DecompInterface ifc = new DecompInterface();
        ifc.toggleCCode(true);
        ifc.openProgram(currentProgram);
        for (int i = 1; i < args.length; i++) {
            Address a = toAddr(args[i]);
            Function f = getFunctionAt(a);
            if (f == null) f = getFunctionContaining(a);
            if (f == null) {
                try { disassemble(a); } catch (Exception e) { println("disasm warn @" + args[i] + ": " + e); }
                try { f = createFunction(a, null); } catch (Exception e) { println("createFn warn @" + args[i] + ": " + e); }
            }
            if (f == null) { println("NO FUNCTION at " + args[i]); continue; }
            DecompileResults res = ifc.decompileFunction(f, 300, monitor);
            String c = (res != null && res.getDecompiledFunction() != null)
                    ? res.getDecompiledFunction().getC() : "// DECOMP FAILED";
            File out = new File(outDir, "fn_" + args[i].replace("0x", "") + ".c");
            try (PrintWriter pw = new PrintWriter(out)) {
                pw.print(c);
            }
            println("dumped " + f.getName() + " @ " + a + " -> " + out.getName());
        }
        ifc.dispose();
    }
}
