const path = require("path");
const p = require(path.join(process.env.TEMP, "syncheck", "node_modules", "@babel", "parser"));
const fs = require("fs");
const files = process.argv.slice(2);
let fail = false;
for (const f of files) {
  try {
    p.parse(fs.readFileSync(f, "utf8"), { sourceType: "module", plugins: ["jsx"] });
    console.log("OK " + f);
  } catch (e) {
    fail = true;
    console.log("ERRO " + f + ": " + e.message);
  }
}
process.exit(fail ? 1 : 0);
