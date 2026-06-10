// List code xrefs (callers) to given addresses, one output file per address.
// Headless args: <outDir> <addr1> [addr2 ...]
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;
import java.io.File;
import java.io.PrintWriter;

public class DumpXrefs extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        File outDir = new File(args[0]);
        outDir.mkdirs();
        for (int i = 1; i < args.length; i++) {
            Address a = toAddr(args[i]);
            File out = new File(outDir, "xrefs_" + args[i].replace("0x", "") + ".txt");
            try (PrintWriter pw = new PrintWriter(out)) {
                ReferenceIterator it = currentProgram.getReferenceManager().getReferencesTo(a);
                int n = 0;
                while (it.hasNext() && n < 500) {
                    Reference r = it.next();
                    Function f = getFunctionContaining(r.getFromAddress());
                    pw.println(r.getFromAddress() + " " + r.getReferenceType()
                            + " in " + (f != null ? f.getName() + " @ " + f.getEntryPoint() : "?"));
                    n++;
                }
                pw.println("total: " + n);
            }
            println("xrefs " + args[i] + " done");
        }
    }
}
