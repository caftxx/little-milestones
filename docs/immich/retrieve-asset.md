# Retrieve an asset

Retrieve detailed information about a specific asset.

**Status:** Stable  
**Auth:** Shared Link  
**Permission:** asset.read

## Request

```
GET /assets/{id}
```

### Function: getAssetInfo

### Parameters

| Name | Required | Type | Description |
|------|----------|------|-------------|
| id | * | UUID | Asset ID |

## Response Fields

| Property | Type | Status | Description |
|----------|------|--------|-------------|
| id | * | UUID | Asset ID |
| key | String | | |
| slug | String | | |
| checksum | * | String | Base64 encoded SHA1 hash |
| createdAt | * | DateTime | The UTC timestamp when the asset was originally uploaded to Immich |
| deviceAssetId | * | String | Device asset ID |
| deviceId | * | String | Device ID |
| duplicateId | String | Null | Duplicate group ID |
| duration | * | String | Video duration (for videos) |
| exifInfo | ExifResponseDto | | |
| fileCreatedAt | * | DateTime | The actual UTC timestamp when the file was created/captured |
| fileModifiedAt | * | DateTime | The UTC timestamp when the file was last modified on the filesystem |
| hasMetadata | * | Boolean | Whether asset has metadata |
| height | * | Number | Null | Asset height |
| isArchived | * | Boolean | Is archived |
| isEdited | * | Boolean | Is edited |
| isFavorite | * | Boolean | Is favorite |
| isOffline | * | Boolean | Is offline |
| isTrashed | * | Boolean | Is trashed |
| libraryId | UUID | Null | Library ID |
| livePhotoVideoId | String | Null | Live photo video ID |
| localDateTime | * | DateTime | The local date and time when the photo/video was taken |
| originalFileName | * | String | Original file name |
| originalMimeType | String | | Original MIME type |
| originalPath | * | String | Original file path |
| owner | UserResponseDto | | |
| ownerId | * | String | Owner user ID |
| people | PersonWithFacesResponseDto[] | | |
| resized | Boolean | | Is resized |
| stack | AssetStackResponseDto | Null | |
| tags | TagResponseDto[] | | |
| thumbhash | * | String | Null | Thumbhash for thumbnail generation |
| type | * | AssetTypeEnum | Asset type |
| unassignedFaces | AssetFaceWithoutPersonResponseDto[] | | |
| updatedAt | * | DateTime | The UTC timestamp when the asset record was last updated |
| visibility | * | AssetVisibility | Asset visibility |
| width | * | Number | Null | Asset width |

## History

- v2: Marked as Status: Stable
- v1: Marked as Status: Beta
- v1: Added

## Live Response

- Method: GET
- URL: Execute