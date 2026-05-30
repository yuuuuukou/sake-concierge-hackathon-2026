targetScope = 'resourceGroup'

@description('azd environment name, such as dev or prod.')
param environmentName string

@description('Azure region for all resources.')
param location string

@description('Short workload name used in Azure resource names.')
param workloadName string

@description('Tags applied to all resources.')
param tags object

@description('Log Analytics data retention in days. 60 days keeps enough history for previous-month reports.')
@minValue(30)
param logAnalyticsRetentionInDays int

@description('Daily Log Analytics ingestion cap in GB. Use -1 for unlimited.')
@minValue(-1)
param logAnalyticsDailyQuotaGb int

@description('Whether to create the Container App instance. Keep false until an application image is available in ACR.')
param deployContainerApp bool

@description('Container image repository name in ACR.')
param containerImageRepository string

@description('Container image tag in ACR.')
param containerImageTag string

@description('Optional full container image reference. When empty, ACR login server/repository/tag is used.')
param containerImageOverride string

@description('Container target port exposed by the FastAPI app.')
param containerPort int

@description('Container CPU allocation. Use 0.25 for the lowest-cost setting.')
param containerCpu string

@description('Container memory allocation. Use 0.5Gi for the lowest-cost setting.')
param containerMemory string

@description('Minimum number of Container App replicas. Use 0 to allow scale-to-zero.')
param containerMinReplicas int

@description('Maximum number of Container App replicas for this environment.')
param containerMaxReplicas int

@description('Microsoft Foundry project endpoint used by the backend.')
param azureAiProjectEndpoint string

@description('Pre-created Foundry Agent name used by the backend.')
param azureAgentName string

@description('Pre-created Foundry Agent version used by the backend.')
param azureAgentVersion string

@description('Per-client /chat request limit in the in-memory FastAPI guard. Set 0 or less to disable.')
param chatRateLimitPerMinute int

@description('Rate limit observation window in seconds for the in-memory FastAPI guard.')
@minValue(1)
param chatRateLimitWindowSeconds int

@description('Per-client feedback / analytics event POST limit in the in-memory FastAPI guard. Set 0 or less to disable.')
param eventRateLimitPerMinute int

@description('Rate limit observation window in seconds for feedback / analytics event POSTs.')
@minValue(1)
param eventRateLimitWindowSeconds int

@description('Public app base URL used for externally advertised URLs such as the A2A Agent Card.')
param publicBaseUrl string

@description('Whether the app should trust X-Forwarded-* headers for client IP detection.')
param trustForwardedHeaders bool

@description('Chat text capture mode: off, feedback_only, or all.')
@allowed([
  'off'
  'feedback_only'
  'all'
])
param chatTextCaptureMode string

@description('Salt used to hash browser session IDs before app logs are written.')
@secure()
param sessionHashSalt string

@description('Store data source used by the backend: local or blob.')
@allowed([
  'local'
  'blob'
])
param storeDataSource string

@description('Blob container name for private store CSV/Markdown data.')
param storeDataBlobContainerName string

@description('Blob prefix for store data. Example: stores/fukunotomo/catalog_master.csv.')
param storeDataBlobPrefix string

@description('Comma-separated store IDs downloaded by the backend when storeDataSource is blob.')
param storeDataStoreIds string

var resourceToken = take(toLower(uniqueString(resourceGroup().id)), 6)
var compactWorkloadName = replace(toLower(workloadName), '-', '')
var compactEnvironmentName = replace(toLower(environmentName), '-', '')
var containerRegistryName = take('${compactWorkloadName}${compactEnvironmentName}${resourceToken}', 50)
var storeDataStorageAccountName = take('${compactWorkloadName}${compactEnvironmentName}data${resourceToken}', 24)
var containerAppName = 'ca-${workloadName}-${environmentName}'
var defaultContainerImage = '${containerRegistry.properties.loginServer}/${containerImageRepository}:${containerImageTag}'
var containerImage = containerImageOverride == '' ? defaultContainerImage : containerImageOverride

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: 'log-${workloadName}-${environmentName}'
  location: location
  tags: tags
  properties: {
    retentionInDays: logAnalyticsRetentionInDays
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    sku: {
      name: 'PerGB2018'
    }
    workspaceCapping: {
      dailyQuotaGb: logAnalyticsDailyQuotaGb
    }
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${workloadName}-${environmentName}'
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: containerRegistryName
  location: location
  sku: {
    name: 'Basic'
  }
  tags: tags
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
    policies: {
      azureADAuthenticationAsArmPolicy: {
        status: 'enabled'
      }
    }
  }
}

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'id-${workloadName}-${environmentName}'
  location: location
  tags: tags
}

resource storeDataStorage 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: storeDataStorageAccountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource storeDataBlobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: storeDataStorage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource storeDataContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = {
  parent: storeDataBlobService
  name: storeDataBlobContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, managedIdentity.id, 'AcrPull')
  scope: containerRegistry
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storeDataReaderRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storeDataContainer.id, managedIdentity.id, 'StorageBlobDataReader')
  scope: storeDataContainer
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
    )
    principalId: managedIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: 'cae-${workloadName}-${environmentName}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2025-07-01' = if (deployContainerApp) {
  name: containerAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: containerPort
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: managedIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'app'
          image: containerImage
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: managedIdentity.properties.clientId
            }
            {
              name: 'AZURE_AIPROJECT_ENDPOINT'
              value: azureAiProjectEndpoint
            }
            {
              name: 'AZURE_AGENT_NAME'
              value: azureAgentName
            }
            {
              name: 'AZURE_AGENT_VERSION'
              value: azureAgentVersion
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: applicationInsights.properties.ConnectionString
            }
            {
              name: 'PORT'
              value: string(containerPort)
            }
            {
              name: 'CHAT_RATE_LIMIT_PER_MINUTE'
              value: string(chatRateLimitPerMinute)
            }
            {
              name: 'CHAT_RATE_LIMIT_WINDOW_SECONDS'
              value: string(chatRateLimitWindowSeconds)
            }
            {
              name: 'EVENT_RATE_LIMIT_PER_MINUTE'
              value: string(eventRateLimitPerMinute)
            }
            {
              name: 'EVENT_RATE_LIMIT_WINDOW_SECONDS'
              value: string(eventRateLimitWindowSeconds)
            }
            {
              name: 'PUBLIC_BASE_URL'
              value: publicBaseUrl
            }
            {
              name: 'TRUST_FORWARDED_HEADERS'
              value: string(trustForwardedHeaders)
            }
            {
              name: 'CHAT_TEXT_CAPTURE_MODE'
              value: chatTextCaptureMode
            }
            {
              name: 'SESSION_HASH_SALT'
              value: sessionHashSalt
            }
            {
              name: 'STORE_DATA_SOURCE'
              value: storeDataSource
            }
            {
              name: 'STORE_DATA_BLOB_ACCOUNT_URL'
              value: storeDataStorage.properties.primaryEndpoints.blob
            }
            {
              name: 'STORE_DATA_BLOB_CONTAINER'
              value: storeDataBlobContainerName
            }
            {
              name: 'STORE_DATA_BLOB_PREFIX'
              value: storeDataBlobPrefix
            }
            {
              name: 'STORE_DATA_STORE_IDS'
              value: storeDataStoreIds
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: containerPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 30
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: containerPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
          resources: {
            cpu: json(containerCpu)
            memory: containerMemory
          }
        }
      ]
      scale: {
        minReplicas: containerMinReplicas
        maxReplicas: containerMaxReplicas
      }
    }
  }
  dependsOn: [
    acrPullRoleAssignment
    storeDataReaderRoleAssignment
  ]
}

output containerRegistryName string = containerRegistry.name
output containerRegistryLoginServer string = containerRegistry.properties.loginServer
output containerAppsEnvironmentName string = containerAppsEnvironment.name
output containerAppsEnvironmentId string = containerAppsEnvironment.id
output managedIdentityName string = managedIdentity.name
output managedIdentityClientId string = managedIdentity.properties.clientId
output applicationInsightsName string = applicationInsights.name
output applicationInsightsConnectionString string = applicationInsights.properties.ConnectionString
output storeDataStorageAccountName string = storeDataStorage.name
output storeDataStorageAccountBlobEndpoint string = storeDataStorage.properties.primaryEndpoints.blob
output storeDataBlobContainerName string = storeDataContainer.name
output containerAppName string = deployContainerApp ? containerApp!.name : ''
output containerAppUrl string = deployContainerApp ? 'https://${containerApp!.properties.configuration.ingress.fqdn}' : ''
