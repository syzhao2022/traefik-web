# cat src/App.vue 
<template>
  <div class="traefik-dashboard">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
      <h1>Service 管理</h1>
      <el-tag :type="wsConnected ? 'success' : 'danger'">
	<span class="tag-content">
	<el-icon><Connection /></el-icon>
        {{ wsConnected ? 'ws已连接' : 'ws断开' }}
	</span>
      </el-tag>
    </div>
    
    <el-card shadow="hover" class="custom-card">
      <template #header>
        <div class="card-header">
          <span>Services 列表</span>
          <el-button type="primary" size="small" @click="refreshServices">
            <el-icon><Refresh /></el-icon> 刷新
          </el-button>
        </div>
      </template>
      
      <el-table 
        :data="services" 
        border 
        stripe 
        style="width: 100%"
        :cell-style="{padding: '8px 6px'}"
        :header-cell-style="{padding: '6px 5px'}"
      >
        <el-table-column prop="service" label="Service名称" width="200" />
        <el-table-column prop="status" label="状态" width="90">
          <template #default="scope">
            <el-tag :type="scope.row.status === 'online' ? 'success' : 'danger'">
              {{ scope.row.status === 'online' ? '在线' : '离线' }}
            </el-tag>
          </template>
        </el-table-column>
        <!--
        <el-table-column prop="totalTraffic" label="总流量(MB)" width="90" />
	-->
        <el-table-column label="流量比例" min-width="320">
          <template #default="scope">
            <el-tag 
              v-for="backend in scope.row.backends" 
              :key="backend.id"
              style="margin: 3px; opacity: 0.9; padding: 6px 10px;"
            >
              {{ backend.namespace }}/{{ backend.name }}: {{ backend.ratio }}%
            </el-tag>
          </template>
        </el-table-column>
	<!--
        <el-table-column label="流量分布" width="200">
          <template #default="scope">
            <div class="mini-chart" ref="miniCharts" :data-chart-id="scope.row.name">
              <echarts 
                :option="getMiniChartOption(scope.row.backends)" 
                autoresize
                style="width: 100%; height: 70px"
              />
            </div>
          </template>
        </el-table-column>
	-->
        <el-table-column prop="updatedAt" label="最后更新" width="190" />
        <el-table-column label="操作" width="140">
          <template #default="scope">
            <el-button 
              type="primary" 
              size="small" 
              @click="openTrafficDialog(scope.row)"
              :disabled="scope.row.status === 'offline'"
              style="padding: 8px 16px;"
            >
              流量配置
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog 
      v-model="trafficDialogVisible" 
      :title="`${currentService?.name || ''} 流量配置`" 
      width="900px"
    >
      <div v-if="currentService">
      <!--
        <div class="chart-container">
          <echarts 
            ref="trafficChart" 
            :option="chartOption" 
            autoresize
          />
        </div>
        -->
        
        <el-form 
          :model="trafficForm" 
          label-width="450px"
          :rules="trafficRules"
          ref="trafficFormRef"
          style="margin-top: 30px"
        >
          <el-form-item 
            v-for="(backend, index) in trafficForm.backends" 
            :key="backend.id"
            :label="`后端 ${backend.namespace} / ${backend.name}`"
            :prop="`backends[${index}].ratio`"
          >
            <el-input-number 
              v-model="backend.ratio" 
              :min="0" 
              :max="100" 
              :precision="0"
              @change="updateChart"
              style="width: 200px"
            />
            <span class="ratio-unit">%</span>
          </el-form-item>
          
          <el-form-item label="总计" style="margin-top: 15px">
            <el-input 
              v-model="totalRatio" 
              disabled 
              suffix="%"
              style="width: 200px"
            />
            <el-tag type="warning" v-if="Math.abs(totalRatio - 100) > 0.1" style="margin-left: 10px">
              需合计为100%
            </el-tag>
            <el-tag type="success" v-else style="margin-left: 10px">
              合计正常
            </el-tag>
          </el-form-item>
        </el-form>
      </div>
      
      <template #footer>
        <el-button @click="trafficDialogVisible = false" style="padding: 8px 20px;">取消</el-button>
        <el-button 
          type="primary" 
          @click="submitTrafficConfig"
          :disabled="Math.abs(totalRatio - 100) > 0.1"
          style="padding: 8px 20px;"
        >
          提交配置
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, watch, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as echarts from 'echarts'
import axios from 'axios'

// WebSocket相关状态
const wsConnected = ref(false)
let ws = null
let reconnectTimer = null
// 新增：提示防抖标记（避免重复提示）
let lastConnectTipTime = 0
let lastUpdateTipTime = 0
const TIP_INTERVAL = 30000 // 提示间隔（30秒）

// 核心状态
const services = ref([])
const currentService = ref(null)
const trafficDialogVisible = ref(false)
const trafficFormRef = ref(null)
const trafficChart = ref(null)
const miniCharts = ref([])
const totalRatio = ref(0)

// 表单定义 & 校验规则
const trafficForm = reactive({
  backends: []
})
const trafficRules = {
  ratio: [
    { required: true, message: '请输入流量比例', trigger: 'blur' },
    { type: 'number', min: 0, max: 100, message: '比例必须在0-100之间', trigger: 'blur' }
  ]
}

// 图表配置
const chartOption = ref({
  title: { text: '后端流量比例分布', left: 'center', textStyle: { color: '#e0e0e0', fontSize: 16 } },
  tooltip: { trigger: 'item', formatter: '{b}: {c}% ({d}%)' },
  legend: { orient: 'vertical', left: 'left', textStyle: { color: '#e0e0e0', fontSize: 14 } },
  series: [{
    name: '流量比例',
    type: 'pie',
    radius: ['40%', '70%'],
    data: [],
    emphasis: { itemStyle: { shadowBlur: 10, shadowOffsetX: 0, shadowColor: 'rgba(0,0,0,0.5)' } },
    label: { color: '#fff', fontSize: 14 }
  }]
})

// 初始化WebSocket连接
const initWebSocket = () => {
  if (ws) {
    ws.close()
  }
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  //const wsUrl = `${wsProtocol}//${window.location.hostname}:8000/ws/traefik-services`
  const wsUrl = `${wsProtocol}//${window.location.hostname}:58000/ws/traefik-services`
  //const wsUrl = `ws://192.168.2.135:58000/ws/traefik-services`
  
  ws = new WebSocket(wsUrl)
  
  ws.onopen = () => {
    wsConnected.value = true
    // 优化：防抖提示，30秒内只提示一次连接成功
    const now = Date.now()
    if (now - lastConnectTipTime > TIP_INTERVAL) {
      ElMessage.success('WebSocket连接成功，实时数据已同步')
      lastConnectTipTime = now
    }
    if (reconnectTimer) {
      clearInterval(reconnectTimer)
      reconnectTimer = null
    }
  }
  
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data)
    console.log(message.type)
    if (message.type === 'full' || message.type === 'update') {
      if (message.type === 'full'){
         services.value = message.data
      }
      if (message.type === 'update'){
	 //console.log(message.data[0].service)
	 const targetItem = services.value.find(
	   item => item.service === message.data[0].service
	 )
	 console.log(targetItem.backends)
	 if (targetItem){
		targetItem.backends = message.data[0].backends
	 }else{
         	services.value.push(message.data[0])
	 }

      }
      console.log(services.value)
      //console.log(services.value)
      // 优化：移除频繁的更新提示，仅在手动刷新时提示
      // 如需保留，添加防抖：30秒内只提示一次
      const now = Date.now()
      if (message.type === 'update' && now - lastUpdateTipTime > TIP_INTERVAL) {
        ElMessage.info('数据已自动更新')
        lastUpdateTipTime = now
      }
    }
    
  }
  
  ws.onclose = () => {
    wsConnected.value = false
    // 优化：重连提示只在首次断开时显示，避免每次重连都提示
    const now = Date.now()
    if (now - lastConnectTipTime > TIP_INTERVAL) {
      ElMessage.warning('WebSocket连接断开，将自动重连')
      lastConnectTipTime = now
    }
    // 优化：重连间隔从5秒增至10秒，减少重连频率
    if (!reconnectTimer) {
      reconnectTimer = setInterval(() => {
        initWebSocket()
      }, 10000)
    }
  }
  
  ws.onerror = (error) => {
    wsConnected.value = false
    // 优化：错误提示防抖
    const now = Date.now()
    if (now - lastConnectTipTime > TIP_INTERVAL) {
      ElMessage.error('WebSocket连接出错')
      lastConnectTipTime = now
    }
    console.error('WebSocket错误:', error)
  }
}

// 刷新列表
const refreshServices = () => {
  fetchServicesFromHttp()
  //if (wsConnected.value) {
  //  ws.send(JSON.stringify({ type: 'refresh' }))
  //  ElMessage.success('已请求刷新Service列表')
  //} else {
  //  fetchServicesFromHttp()
  //}
}

// 从HTTP接口获取数据（备用）
const fetchServicesFromHttp = async () => {
  try {
    //const http_api = `${window.location.protocol}//${window.location.hostname}:58000/api/traefik-services`
    //const response = await axios.get('http://localhost:8000/api/traefik-services')
    const response = await axios.get("/api/traefik-services")
    if (response.data.code === 200) {
      services.value = response.data.data
      ElMessage.success('从HTTP接口刷新Service列表成功')
    }
  } catch (error) {
    ElMessage.error('刷新失败：' + error.message)
  }
}

// 获取迷你饼图配置
const getMiniChartOption = (backends) => {
  const data = backends.map(item => ({ name: item.name, value: item.ratio }))
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c}%' },
    series: [{
      type: 'pie',
      radius: ['30%', '80%'],
      data: data,
      label: { show: false },
      labelLine: { show: false },
      silent: true
    }],
    color: ['#409eff', '#67c23a', '#e6a23c', '#f56c6c', '#909399'],
    backgroundColor: 'transparent'
  }
}

// 打开流量配置弹窗
const openTrafficDialog = (service) => {
  currentService.value = service
  console.log(service.service)
  trafficForm.backends = JSON.parse(JSON.stringify(service.backends))
  updateChart()
  trafficDialogVisible.value = true
}

// 更新图表
const updateChart = () => {
  totalRatio.value = trafficForm.backends.reduce((sum, item) => sum + Number(item.ratio || 0), 0)
  chartOption.value.series[0].data = trafficForm.backends.map(item => ({
    name: item.name,
    value: item.ratio || 0
  }))
  if (trafficChart.value) {
    const chart = echarts.getInstanceByDom(trafficChart.value)
    if (!chart) echarts.init(trafficChart.value).setOption(chartOption.value)
    else chart.setOption(chartOption.value)
  }
}

// 提交流量配置
const submitTrafficConfig = async () => {
  try {
    await trafficFormRef.value.validate()
    
    if (Math.abs(totalRatio.value - 100) > 0.1) {
      ElMessage.warning('流量比例总和必须等于100%')
      return
    }
    
    console.log(currentService.value.service)
    const response = await axios.post(
      '/api/update-traffic-config',
      {
        service_name: currentService.value.service,
        backends: trafficForm.backends
      }
    )
    
    if (response.data.code === 200) {
      ElMessage.success(response.data.message)
      trafficDialogVisible.value = false
    } else {
      ElMessage.error(response.data.message || '提交失败')
    }
    
  } catch (error) {
    console.error('提交失败:', error)
    ElMessage.error('提交失败：' + (error.response?.data?.detail || error.message))
  }
}

// 生命周期
onMounted(() => {
  initWebSocket()
  watch(trafficDialogVisible, (visible) => {
    if (visible) setTimeout(() => updateChart(), 100)
  })
})

onUnmounted(() => {
  if (ws) {
    ws.close()
  }
  if (reconnectTimer) {
    clearInterval(reconnectTimer)
  }
})
</script>

<style scoped>
.traefik-dashboard {
  padding: 30px;
  max-width: 98%;
  margin: 0 auto;
}

.custom-card {
  width: 100%;
  min-height: 600px;
  padding: 12px;
  box-sizing: border-box;
}

.tag-content {
  display: inline-flex;
  align-items: center;
  gap: 6px; /* 图标和文字之间的间距，可微调 */
}


.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.chart-container {
  width: 100%;
  height: 450px;
}

.ratio-unit {
  margin-left: 10px;
  color: #999;
  font-size: 14px;
}

.mini-chart {
  width: 100%;
  height: 70px;
  display: flex;
  align-items: center;
  justify-content: center;
}

:deep(.el-table) {
  --el-table-bg-color: #242424;
  --el-table-row-hover-bg-color: #333;
  --el-table-header-text-color: #e0e0e0;
  --el-table-text-color: #ddd;
  font-size: 14px;
}

:deep(.el-table th) {
  font-weight: 600;
}

:deep(.el-dialog) {
  --el-dialog-bg-color: #242424;
  --el-dialog-title-color: #e0e0e0;
  --el-dialog-font-size: 14px;
}

:deep(.el-form-item__label) {
  color: #ddd;
  font-size: 14px;
}

:deep(.el-button) {
  font-size: 14px;
}

:deep(.el-tag) {
  font-size: 14px;
}
</style>
