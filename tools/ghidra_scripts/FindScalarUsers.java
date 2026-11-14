// Scan every instruction for operands carrying the given scalar values (e.g. struct displacements)
// and report, per function, which of the scalars it touches and how often. Use it to locate the
// CONSUMERS of a struct once a loader has revealed the field offsets (a species record's +0x9b0, say).
// Headless args: <outDir> <scalar1> [scalar2 ...]   (hex 0x... or decimal). Writes scalar_users.txt:
// functions sorted by (#distinct scalars matched desc, total hits desc).
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.scalar.Scalar;
import java.io.File;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

public class FindScalarUsers extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        File outDir = new File(args[0]);
        outDir.mkdirs();
        long[] wanted = new long[args.length - 1];
        for (int i = 1; i < args.length; i++) {
            String s = args[i].toLowerCase();
            wanted[i - 1] = s.startsWith("0x") ? Long.parseLong(s.substring(2), 16) : Long.parseLong(s);
        }
        // fn entry -> (scalar -> count)
        Map<Address, TreeMap<Long, Integer>> hits = new HashMap<>();
        Map<Address, String> names = new HashMap<>();
        InstructionIterator it = currentProgram.getListing().getInstructions(true);
        long scanned = 0;
        while (it.hasNext() && !monitor.isCancelled()) {
            Instruction ins = it.next();
            scanned++;
            int nops = ins.getNumOperands();
            for (int op = 0; op < nops; op++) {
                Object[] objs = ins.getOpObjects(op);
                for (Object o : objs) {
                    if (!(o instanceof Scalar)) continue;
                    long v = ((Scalar) o).getValue();
                    long uv = ((Scalar) o).getUnsignedValue();
                    for (long w : wanted) {
                        if (v != w && uv != w) continue;
                        Function f = getFunctionContaining(ins.getAddress());
                        Address key = f != null ? f.getEntryPoint() : ins.getAddress();
                        if (f != null) names.put(key, f.getName());
                        hits.computeIfAbsent(key, k -> new TreeMap<>()).merge(w, 1, Integer::sum);
                    }
                }
            }
        }
        List<Map.Entry<Address, TreeMap<Long, Integer>>> rows = new ArrayList<>(hits.entrySet());
        rows.sort((a, b) -> {
            int d = Integer.compare(b.getValue().size(), a.getValue().size());
            if (d != 0) return d;
            int ta = a.getValue().values().stream().mapToInt(Integer::intValue).sum();
            int tb = b.getValue().values().stream().mapToInt(Integer::intValue).sum();
            return Integer.compare(tb, ta);
        });
        try (PrintWriter pw = new PrintWriter(new File(outDir, "scalar_users.txt"))) {
            pw.println("scanned instructions: " + scanned + "; functions with hits: " + rows.size());
            for (Map.Entry<Address, TreeMap<Long, Integer>> e : rows) {
                StringBuilder sb = new StringBuilder();
                sb.append(e.getKey()).append(' ').append(names.getOrDefault(e.getKey(), "?"));
                sb.append("  distinct=").append(e.getValue().size()).append("  ");
                for (Map.Entry<Long, Integer> h : e.getValue().entrySet())
                    sb.append(String.format("0x%x:%d ", h.getKey(), h.getValue()));
                pw.println(sb);
            }
        }
        println("FindScalarUsers done: " + rows.size() + " functions");
    }
}
