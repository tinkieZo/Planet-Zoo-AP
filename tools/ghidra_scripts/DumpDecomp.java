// Decompile functions at given addresses to .c files.
// Headless args: <outDir> <addr1> [addr2 ...]
// GUI (Script Manager): no args needed - uses the DEFAULTS below.
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import java.io.File;
import java.io.PrintWriter;

public class DumpDecomp extends GhidraScript {
    // GUI-mode fallback addresses; the output dir comes from the PZ_DECOMP_OUT env var
    // (point it at <repo>\tools\_decomp), else ~/pz_decomp.
    private static final String[] DEFAULT_ADDRS = { "0x140E3FBF0", "0x1468CBF30" };

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length == 0) {
            String out = System.getenv("PZ_DECOMP_OUT");
            if (out == null || out.isEmpty()) {
                out = new File(System.getProperty("user.home"), "pz_decomp").getPath();
            }
            args = new String[DEFAULT_ADDRS.length + 1];
            args[0] = out;
            System.arraycopy(DEFAULT_ADDRS, 0, args, 1, DEFAULT_ADDRS.length);
        }
        File outDir = new File(args[0]);
        outDir.mkdirs();
        DecompInterface ifc = new DecompInterface();
        ifc.openProgram(currentProgram);
        for (int i = 1; i < args.length; i++) {
            Address a = toAddr(args[i]);
            Function f = getFunctionAt(a);
            if (f == null) f = getFunctionContaining(a);
            if (f == null) { println("NO FUNCTION at " + args[i]); continue; }
            DecompileResults res = ifc.decompileFunction(f, 180, monitor);
            String c = (res != null && res.getDecompiledFunction() != null)
                    ? res.getDecompiledFunction().getC() : "// DECOMP FAILED";
            File out = new File(outDir, "fn_" + args[i].replace("0x", "") + ".c");
            try (PrintWriter pw = new PrintWriter(out)) {
                pw.print(c);
            }
            println("dumped " + f.getName() + " -> " + out.getName());
        }
        ifc.dispose();
    }
}
