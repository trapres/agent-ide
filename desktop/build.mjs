import { build } from 'esbuild';
import { copyFile, mkdir } from 'node:fs/promises';
await mkdir('dist', { recursive: true });
await copyFile('src/index.html', 'dist/index.html');
await build({ entryPoints: ['src/app.js'], bundle: true, outfile: 'dist/app.js',
  format: 'esm', target: 'safari15', loader: { '.css': 'css' } });
