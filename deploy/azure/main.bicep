// ============================================================================
// TonerWatch — Azure App Service deployment (Bicep)
// ============================================================================
// Provisions:
//   * Storage account + Azure Files share mounted at /data on the container
//     (holds the SQLite database and the Fernet encryption key)
//   * Linux App Service Plan
//   * Linux Web App that pulls the container image from GHCR
//
// Compile to ARM with:  az bicep build --file main.bicep
// Deploy with:          az deployment group create -g <rg> \
//                          --template-file main.bicep -p appName=<name>
// ============================================================================

@description('Unique app name. The site will be reachable at https://<appName>.azurewebsites.net')
param appName string

@description('Azure region — defaults to the resource group location')
param location string = resourceGroup().location

@allowed([ 'F1', 'B1', 'B2', 'B3', 'S1', 'P1V3' ])
@description('App Service Plan SKU. B1 recommended (~10 EUR/month, always-on).')
param sku string = 'B1'

@description('Container image to pull from the registry.')
param containerImage string = 'ghcr.io/mnimtz/tonerwatch:latest'

@description('IANA timezone for the runtime — controls quiet-hours calculations.')
param tz string = 'Europe/Berlin'

@allowed([ 'en', 'fr', 'it', 'de', 'es' ])
@description('Fallback UI language when the browser preference cannot be resolved.')
param defaultLang string = 'en'

var planName     = '${appName}-plan'
var storageName  = 'tonrad${uniqueString(resourceGroup().id)}'
var fileShareName = 'tonerwatch-data'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  name: '${storage.name}/default/${fileShareName}'
  properties: { shareQuota: 5 }
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  sku: { name: sku }
  kind: 'linux'
  properties: { reserved: true }
}

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
  kind: 'app,linux,container'
  dependsOn: [ share ]
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'DOCKER|${containerImage}'
      alwaysOn: sku != 'F1'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'WEBSITES_PORT', value: '8080' }
        { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'false' }
        { name: 'DOCKER_REGISTRY_SERVER_URL', value: 'https://ghcr.io' }
        { name: 'WEB_HOST', value: '0.0.0.0' }
        { name: 'WEB_PORT', value: '8080' }
        { name: 'DB_PATH', value: '/data/tonerwatch.sqlite' }
        { name: 'DEFAULT_LANG', value: defaultLang }
        { name: 'TZ', value: tz }
      ]
      azureStorageAccounts: {
        data: {
          type: 'AzureFiles'
          accountName: storageName
          shareName: fileShareName
          mountPath: '/data'
          accessKey: listKeys(storage.id, '2023-05-01').keys[0].value
        }
      }
    }
  }
}

output appUrl string        = 'https://${appName}.azurewebsites.net'
output storageAccount string = storageName
output nextSteps string = 'Open the App URL in your browser, complete the first-run setup wizard to create the admin account, then add your first Printix customer under /customers.'
