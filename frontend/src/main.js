import { createApp } from 'vue'
// Element Plus 已按需引入（unplugin-vue-components）；仅保留基础 CSS 变量
import 'element-plus/theme-chalk/base.css'
import App from './App.vue'
import './styles.css'

createApp(App).mount('#app')
