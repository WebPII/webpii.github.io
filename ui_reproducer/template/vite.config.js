import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import fs from 'fs'

// NOTE: These paths are set by reproduce_ui.py - do not modify
const productsDir = path.resolve(__dirname, '__PRODUCTS_PATH__')

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
  publicDir: path.resolve(__dirname, '__ASSETS_PATH__'),
  resolve: {
    alias: {
      // Shared data.json for PII/product placeholders
      '@data': path.resolve(__dirname, '__DATA_PATH__'),
      // Runtime generators for IDs, cards, tracking numbers
      '@generators': path.resolve(__dirname, 'src/generators.js')
    }
  }
})
