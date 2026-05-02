import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import fs from 'fs'

// reproduce_ui.py replaces these placeholders for generated UI projects.
// In the released template, fall back to example_data so reviewers can build it.
function resolvePlaceholderPath(placeholder, fallback) {
  const candidate = path.resolve(__dirname, placeholder)
  return fs.existsSync(candidate) ? candidate : path.resolve(__dirname, fallback)
}

const productsDir = resolvePlaceholderPath('__PRODUCTS_PATH__', '../../example_data/assets')
const assetsDir = resolvePlaceholderPath('__ASSETS_PATH__', '../../example_data/assets_lite')
const dataPath = resolvePlaceholderPath('__DATA_PATH__', 'src/data.json')

export default defineConfig({
  plugins: [
    react(),
    // Serve product images on-demand without scanning all 400K+ files
    {
      name: 'serve-products',
      configureServer(server) {
        server.middlewares.use('/products', (req, res, next) => {
          const filePath = path.join(productsDir, req.url)
          if (fs.existsSync(filePath)) {
            const ext = path.extname(filePath).toLowerCase()
            const mimeTypes = {
              '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
              '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp'
            }
            res.setHeader('Content-Type', mimeTypes[ext] || 'application/octet-stream')
            fs.createReadStream(filePath).pipe(res)
          } else {
            next()
          }
        })
      }
    }
  ],
  // External assets directory (company logos, payment methods - NOT products)
  // Products are served via middleware above to avoid Vite scanning 400K+ files
  publicDir: assetsDir,
  resolve: {
    alias: {
      // Shared data.json for PII/product placeholders
      '@data': dataPath,
      // Runtime generators for IDs, cards, tracking numbers
      '@generators': path.resolve(__dirname, 'src/generators.js')
    }
  }
})
