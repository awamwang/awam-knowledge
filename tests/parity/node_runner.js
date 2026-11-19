'use strict';
// SPDX-License-Identifier: MIT
// parity runner：调用现有 Node bm25-index.js，输出 JSON 给 Python 端比对。
// 用法： node node_runner.js tokens <stem0|1> <text...>
//        node node_runner.js search <root> <stem0|1> <query>
const path = require('path');
const bm = require(path.join(__dirname, '..', '..', 'code', 'bm25-index.js'));

const mode = process.argv[2];
if (mode === 'tokens') {
  const stem = process.argv[3] === '1';
  const text = process.argv.slice(4).join(' ');
  process.stdout.write(JSON.stringify(bm.tokenize(text, stem)));
} else if (mode === 'search') {
  const root = process.argv[3];
  const stem = process.argv[4] === '1';
  const query = process.argv[5];
  const corpus = bm.buildCorpus({ root, stem });
  const idx = bm.buildIndex(corpus.docs);
  const w = bm.queryWeights(query, { stem });
  const ranked = bm.rankAll(corpus.docs, idx, w)
    .map(r => ({ path: r.path, line: r.lineStart, score: r.score }));
  process.stdout.write(JSON.stringify(ranked));
} else {
  process.stderr.write('unknown mode\n');
  process.exit(2);
}
