targetScope = 'subscription'

@description('azd environment name, such as dev or prod.')
@minLength(2)
@maxLength(16)
param environmentName string

@description('Azure region for all resources.')
param location string = deployment().location

@description('Short workload name used in Azure resource names.')
@minLength(2)
@maxLength(32)
param workloadName string = 'sake-concierge'

@description('Optional tags applied to all resources.')
param tags object = {}

@description('Log Analytics data retention in days. 60 days keeps enough history for previous-month reports.')
@minValue(30)
param logAnalyticsRetentionInDays int = 60

@description('Daily Log Analytics ingestion cap in GB. Use -1 for unlimited.')
@minValue(-1)
param logAnalyticsDailyQuotaGb int = 1

@description('Whether to create the Container App instance. Keep false until an application image is available in ACR.')
param deployContainerApp bool = false

@description('Container image repository name in ACR.')
param containerImageRepository string = 'sake-concierge'

@description('Container image tag in ACR.')
param containerImageTag string = 'latest'

@description('Optional full container image reference. When empty, ACR login server/repository/tag is used.')
param containerImageOverride string = ''

@description('Container target port exposed by the FastAPI app.')
param containerPort int = 8000

@description('Container CPU allocation. Use 0.25 for the lowest-cost setting.')
@allowed([
  '0.25'
  '0.5'
  '0.75'
  '1.0'
])
param containerCpu string = '0.25'

@description('Container memory allocation. Use 0.5Gi for the lowest-cost setting.')
@allowed([
  '0.5Gi'
  '1Gi'
  '1.5Gi'
  '2Gi'
])
param containerMemory string = '0.5Gi'

@description('Minimum number of Container App replicas. Use 0 to allow scale-to-zero.')
@minValue(0)
param containerMinReplicas int = 1

@description('Maximum number of Container App replicas for this environment.')
@minValue(1)
param containerMaxReplicas int = 1

@description('Microsoft Foundry project endpoint used by the backend.')
param azureAiProjectEndpoint string = ''

@description('Pre-created Foundry Agent name used by the backend.')
param azureAgentName string = 'sake-concierge'

@description('Pre-created Foundry Agent version used by the backend.')
param azureAgentVersion string = ''

@description('Per-client /chat request limit in the in-memory FastAPI guard. Set 0 or less to disable.')
param chatRateLimitPerMinute int = 5

@description('Rate limit observation window in seconds for the in-memory FastAPI guard.')
@minValue(1)
param chatRateLimitWindowSeconds int = 60

@description('Per-client feedback / analytics event POST limit in the in-memory FastAPI guard. Set 0 or less to disable.')
param eventRateLimitPerMinute int = 60

@description('Rate limit observation window in seconds for feedback / analytics event POSTs.')
@minValue(1)
param eventRateLimitWindowSeconds int = 60

@description('Public app base URL used for externally advertised URLs such as the A2A Agent Card.')
param publicBaseUrl string = ''

@description('Whether the app should trust X-Forwarded-* headers for client IP detection. Keep false unless the ingress path is known to sanitize these headers.')
param trustForwardedHeaders bool = false

@description('Chat text capture mode: off, feedback_only, or all. Dev/prod default to feedback_only.')
@allowed([
  'off'
  'feedback_only'
  'all'
])
param chatTextCaptureMode string = 'feedback_only'

@description('Salt used to hash browser session IDs before app logs are written. Set a random value per environment.')
@secure()
param sessionHashSalt string = ''

@description('Store data source used by the backend: local or blob.')
@allowed([
  'local'
  'blob'
])
param storeDataSource string = 'local'

@description('Blob container name for private store CSV/Markdown data.')
param storeDataBlobContainerName string = 'store-data'

@description('Blob prefix for store data. Example: stores/fukunotomo/catalog_master.csv.')
param storeDataBlobPrefix string = 'stores'

@description('Comma-separated store IDs downloaded by the backend when storeDataSource is blob.')
param storeDataStoreIds string = 'fukunotomo'

var defaultTags = {
  application: 'sake-concierge'
  environment: environmentName
  managedBy: 'azd'
}
var resourceTags = union(defaultTags, tags)
var resourceGroupName = 'rg-${workloadName}-${environmentName}'

resource resourceGroup 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: resourceGroupName
  location: location
  tags: resourceTags
}

module host 'core/host.bicep' = {
  name: 'host-${environmentName}'
  scope: resourceGroup
  params: {
    environmentName: environmentName
    location: location
    workloadName: workloadName
    tags: resourceTags
    logAnalyticsRetentionInDays: logAnalyticsRetentionInDays
    logAnalyticsDailyQuotaGb: logAnalyticsDailyQuotaGb
    deployContainerApp: deployContainerApp
    containerImageRepository: containerImageRepository
    containerImageTag: containerImageTag
    containerImageOverride: containerImageOverride
    containerPort: containerPort
    containerCpu: containerCpu
    containerMemory: containerMemory
    containerMinReplicas: containerMinReplicas
    containerMaxReplicas: containerMaxReplicas
    azureAiProjectEndpoint: azureAiProjectEndpoint
    azureAgentName: azureAgentName
    azureAgentVersion: azureAgentVersion
    chatRateLimitPerMinute: chatRateLimitPerMinute
    chatRateLimitWindowSeconds: chatRateLimitWindowSeconds
    eventRateLimitPerMinute: eventRateLimitPerMinute
    eventRateLimitWindowSeconds: eventRateLimitWindowSeconds
    publicBaseUrl: publicBaseUrl
    trustForwardedHeaders: trustForwardedHeaders
    chatTextCaptureMode: chatTextCaptureMode
    sessionHashSalt: sessionHashSalt
    storeDataSource: storeDataSource
    storeDataBlobContainerName: storeDataBlobContainerName
    storeDataBlobPrefix: storeDataBlobPrefix
    storeDataStoreIds: storeDataStoreIds
  }
}

output resourceGroupName string = resourceGroup.name
output containerRegistryName string = host.outputs.containerRegistryName
output containerRegistryLoginServer string = host.outputs.containerRegistryLoginServer
output containerAppsEnvironmentName string = host.outputs.containerAppsEnvironmentName
output containerAppsEnvironmentId string = host.outputs.containerAppsEnvironmentId
output managedIdentityName string = host.outputs.managedIdentityName
output managedIdentityClientId string = host.outputs.managedIdentityClientId
output applicationInsightsName string = host.outputs.applicationInsightsName
output applicationInsightsConnectionString string = host.outputs.applicationInsightsConnectionString
output storeDataStorageAccountName string = host.outputs.storeDataStorageAccountName
output storeDataStorageAccountBlobEndpoint string = host.outputs.storeDataStorageAccountBlobEndpoint
output storeDataBlobContainerName string = host.outputs.storeDataBlobContainerName
output containerAppName string = host.outputs.containerAppName
output containerAppUrl string = host.outputs.containerAppUrl
