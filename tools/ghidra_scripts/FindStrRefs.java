// Find defined strings containing each needle, list code xrefs to them, and decompile the
// referencing functions. Headless args: <outDir> <needle1> [needle2 ...]
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;
import java.io.File;
import java.io.PrintWriter;
import java.util.LinkedHashSet;
import java.util.Set;

public class FindStrRefs extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        File outDir = new File(args[0]);
        outDir.mkdirs();
        DecompInterface ifc = new DecompInterface();
        ifc.openProgram(currentProgram);
        PrintWriter idx = new PrintWriter(new File(outDir, "index.txt"));
        Set<Function> fns = new LinkedHashSet<>();
        int matched = 0;
        DataIterator di = currentProgram.getListing().getDefinedData(true);
        while (di.hasNext()) {
            Data d = di.next();
            Object v;
            try { v = d.getValue(); } catch (Exception e) { continue; }
            if (!(v instanceof String)) continue;
            String s = (String) v;
            boolean hit = false;
            for (int i = 1; i < args.length; i++) if (s.contains(args[i])) { hit = true; break; }
            if (!hit) continue;
            matched++;
            idx.println("STRING @" + d.getAddress() + " : " + s);
            ReferenceIterator it = currentProgram.getReferenceManager().getReferencesTo(d.getAddress());
            int n = 0;
            while (it.hasNext() && n < 60) {
                Reference r = it.next(); n++;
                Function f = getFunctionContaining(r.getFromAddress());
                idx.println("   xref " + r.getFromAddress() + " in "
                        + (f != null ? f.getName() + " @ " + f.getEntryPoint() : "?"));
                if (f != null) fns.add(f);
            }
            if (matched >= 30) break;
        }
        idx.println("matched strings: " + matched + ", referencing functions: " + fns.size());
        idx.close();
        for (Function f : fns) {
            DecompileResults res = ifc.decompileFunction(f, 120, monitor);
            String c = (res != null && res.getDecompiledFunction() != null)
                    ? res.getDecompiledFunction().getC() : "// DECOMP FAILED";
            try (PrintWriter pw = new PrintWriter(new File(outDir, "fn_" + f.getEntryPoint() + ".c"))) {
                pw.print(c);
            }
            println("decompiled " + f.getName() + " @ " + f.getEntryPoint());
        }
        ifc.dispose();
        println("FindStrRefs done: " + matched + " strings, " + fns.size() + " functions");
    }
}
