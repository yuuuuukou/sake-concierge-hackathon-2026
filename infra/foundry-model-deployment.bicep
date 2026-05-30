targetScope = 'resourceGroup'

@description('Existing Microsoft Foundry / Azure OpenAI account name.')
param accountName string

@description('Model deployment name.')
param deploymentName string

@description('OpenAI model name.')
param modelName string = deploymentName

@description('OpenAI model version.')
param modelVersion string

@description('Deployment SKU name.')
param deploymentSkuName string = 'GlobalStandard'

@description('Deployment capacity units. Choose a value that fits your quota and demo traffic.')
@minValue(1)
param deploymentSkuCapacity int = 1

@description('Responsible AI policy applied to the deployment.')
param raiPolicyName string = 'Microsoft.DefaultV2'

@description('Model version upgrade behavior.')
@allowed([
  'NoAutoUpgrade'
  'OnceCurrentVersionExpired'
  'OnceNewDefaultVersionAvailable'
])
param versionUpgradeOption string = 'OnceNewDefaultVersionAvailable'

resource account 'Microsoft.CognitiveServices/accounts@2026-03-01' existing = {
  name: accountName
}

resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2026-03-01' = {
  name: deploymentName
  parent: account
  sku: {
    name: deploymentSkuName
    capacity: deploymentSkuCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    raiPolicyName: raiPolicyName
    versionUpgradeOption: versionUpgradeOption
  }
}

output modelDeploymentName string = modelDeployment.name
output modelDeploymentCapacity int = deploymentSkuCapacity
