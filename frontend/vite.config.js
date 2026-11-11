import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({ resolvers: [ElementPlusResolver()] }),
    Components({ resolvers: [ElementPlusResolver()] })
  ],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      // 后端 FastAPI（DmfDataAgent，uvicorn 默认 8000）
      // 生产部署时由 FastAPI 静态托管 frontend/dist，同源无需代理
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://localhost:8000',
        changeOrigin: true
      }
    }
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      output: {
        manualChunks: {
          // 仅 echarts 按需入口拆独立 chunk（缓存友好）；
          // 注意：element-plus 不做数组分组——数组式 manualChunks 会禁用 tree-shaking 拉入全量
          echarts: ['echarts/core', 'echarts/charts', 'echarts/components', 'echarts/renderers']
        }
      }
    }
  }
})
