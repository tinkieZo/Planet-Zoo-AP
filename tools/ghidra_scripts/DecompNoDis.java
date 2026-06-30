// Decompile functions WITHOUT an explicit follow-flow disassemble (that cascades into callees on this
// obfuscated binary). createFunction does body-only disassembly; the decompiler handles the rest.
// Headless args: <outDir> <addr1> [addr2 ...]
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import java.io.File;
import java.io.PrintWriter;

public class DecompNoDis extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        File outDir = new File(args[0]); outDir.mkdirs();
        DecompInterface ifc = new DecompInterface();
        ifc.toggleCCode(true); ifc.openProgram(currentProgram);
        for (int i = 1; i < args.length; i++) {
            Address a = toAddr(args[i]);
            Function f = getFunctionAt(a);
            if (f == null) f = getFunctionContaining(a);
            if (f == null) { try { f = createFunction(a, null); } catch (Exception e) { println("createFn "+args[i]+": "+e); } }
            if (f == null) { println("NO FUNCTION at " + args[i]); continue; }
            DecompileResults res = ifc.decompileFunction(f, 240, monitor);
            String c = (res != null && res.getDecompiledFunction() != null) ? res.getDecompiledFunction().getC() : "// FAIL";
            try (PrintWriter pw = new PrintWriter(new File(outDir, "fn_" + args[i].replace("0x","") + ".c"))) { pw.print(c); }
            println("dumped " + f.getName() + " @ " + a);
        }
        ifc.dispose();
    }
}
