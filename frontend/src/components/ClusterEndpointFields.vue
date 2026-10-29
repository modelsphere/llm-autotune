<script setup lang="ts">
/** How AutoTune and LLMBench reach a cluster's runs. The cluster's location
 *  (`sameCluster`, see ClusterLocation) decides what is offered: a Service name
 *  resolves only inside AutoTune's own cluster, so an external one is locked
 *  to NodePort. */
import { Lock } from '@element-plus/icons-vue'
import ClusterLocation from './ClusterLocation.vue'
import InfoHint from './InfoHint.vue'
import { useI18n } from '../i18n'

const props = defineProps<{
  workloadKind: string
  sameCluster?: boolean | null
  detecting?: boolean
}>()
const host = defineModel<string>('host', { required: true })
const port = defineModel<number>('port', { required: true })
const endpoint = defineModel<string>('endpoint', { default: 'dns' })
const { t } = useI18n()
</script>

<template>
  <el-form-item v-if="workloadKind === 'custom'">
    <template #label>
      {{ t('resources.endpointMode') }}
      <ClusterLocation :same="props.sameCluster" :detecting="props.detecting" class="loc" />
    </template>
    <span class="muted small">{{ t('resources.customEndpointHint') }}</span>
  </el-form-item>
  <template v-else>
    <div class="grid-2">
      <el-form-item>
        <template #label>
          {{ t('resources.endpointMode') }}
          <ClusterLocation :same="props.sameCluster" :detecting="props.detecting" class="loc" />
        </template>
        <el-radio-group v-if="workloadKind === 'llmservice'" v-model="endpoint">
          <el-tooltip :content="t('resources.loc.externalTip')" :disabled="props.sameCluster !== false"
            placement="top">
            <el-radio value="dns" :disabled="props.sameCluster === false">
              <el-icon v-if="props.sameCluster === false" class="lock"><Lock /></el-icon>
              {{ t('resources.endpointDns') }}
            </el-radio>
          </el-tooltip>
          <el-radio value="nodeport">{{ t('resources.endpointNodeport') }}</el-radio>
        </el-radio-group>
        <span v-else class="small">{{ t('resources.endpointNodeport') }}</span>
      </el-form-item>
      <el-form-item v-if="workloadKind !== 'llmservice' || endpoint === 'nodeport'">
        <template #label>
          {{ t('resources.endpointHost') }}
          <InfoHint>{{ t('resources.endpointHostHint') }}</InfoHint>
        </template>
        <el-input v-model.trim="host" class="mono" :placeholder="t('resources.endpointHostPlaceholder')" />
      </el-form-item>
    </div>
    <template v-if="workloadKind === 'deployment'">
      <el-form-item>
        <template #label>
          {{ t('resources.serviceNodePort') }}
          <InfoHint>{{ t('resources.serviceNodePortHint') }}</InfoHint>
        </template>
        <el-input-number v-model="port" :min="0" :max="65535" :precision="0"
          @change="port = $event ?? 0" />
        <span v-if="port > 0" class="warn small">{{ t('resources.fixedNodePortHint') }}</span>
      </el-form-item>
    </template>
  </template>
</template>

<style scoped>
.loc {
  margin-left: 6px;
}
.lock {
  margin-right: 2px;
  vertical-align: -2px;
}
.warn {
  margin-left: 10px;
  color: var(--el-color-warning);
}
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 16px;
}
@media (max-width: 560px) {
  .grid-2 {
    grid-template-columns: 1fr;
  }
}
</style>
