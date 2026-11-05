# App Details

## Changes

### 1.0.0
- Adding Desktop support via [EC200U Module](https://www.quectel.com/product/lte-ec200u-series/) and technically should work with other such modules via the `AT` commands.
- Both CLI & GUI can be used from Desktop platform. See the [README](./README.md) for more details.

### 0.3.0
- Service type is sticky now, should restart automatically unless stopped. [android]
- Service should run even when app is closed. [android]
- Added a stop button at notification to stop the service. [android]

### 0.2.1
- Fixing permission requirement as per Android version.
- Moving from DATA_SYNC to SPECIAL_USE service type.
- A single config source for the entire app.

### 0.2.0
- Adding a check for correct mobile number formatting.
- SMS in parts if it exits character limit.
- Showing the API endpoints and API Doc button once server is started.
- Some UI updates.

### 0.1.1
- Fixing app crash on some mobile GPU with kivymd 2.x . Thanks to [DorDeorz](https://github.com/DorDeorz) for the fix in [UniversalDownlaoder](https://github.com/DorDeorz/UniversalDownloader).

### 0.1.0
- App will check if required permissions are granted
- Adding logging for API requests
- Fixing the android bottom padding
- Adding a back button

### 0.0.1
- Initial version
