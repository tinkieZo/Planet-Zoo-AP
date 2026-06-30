// preScript: trim the RAM/time-heavy analyzers so a huge binary analyzes within a modest heap (enough
// for function discovery + references, which is all FindStrRefs/DumpXrefs need; on-demand decompilation
// still works via DecompInterface regardless of the Decompiler Parameter ID analyzer).
import ghidra.app.script.GhidraScript;

public class SetAnalysisOpts extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] off = {
            "Decompiler Parameter ID",
            "Decompiler Switch Analysis",
            "Aggressive Instruction Finder",
            "Call Convention ID",
            "Shared Return Calls",
        };
        for (String o : off) {
            try { setAnalysisOption(currentProgram, o, "false"); } catch (Exception e) { println("opt " + o + ": " + e); }
        }
        println("analysis options trimmed for low-RAM run");
    }
}
