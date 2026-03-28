# Search assets by metadata

搜索资源基于各种元数据条件。

**状态**: Stable
**权限**: asset.read

## 端点信息

**方法**: POST
**路径**: `/search/metadata`
**名称**: searchAssets

---

## 请求参数

| 参数名 | 类型 | 说明 |
|--------|------|------|
| `albumIds` | UUID[] | 按相册 ID 过滤 |
| `checksum` | String | 按文件校验和过滤 |
| `city` | String \| Null | 按城市名称过滤 |
| `country` | String \| Null | 按国家名称过滤 |
| `createdAfter` | DateTime | 按创建日期过滤（之后） |
| `createdBefore` | DateTime | 按创建日期过滤（之前） |
| `description` | String | 按描述文本过滤 |
| `deviceAssetId` | String | 按设备资源 ID 过滤 |
| `deviceId` | String | 按设备 ID 过滤 |
| `encodedVideoPath` | String | 按编码视频文件路径过滤 |
| `id` | UUID | 按资源 ID 过滤 |
| `isEncoded` | Boolean | 按编码状态过滤 |
| `isFavorite` | Boolean | 按收藏状态过滤 |
| `isMotion` | Boolean | 按动态照片状态过滤 |
| `isNotInAlbum` | Boolean | 过滤不在任何相册中的资源 |
| `isOffline` | Boolean | 按离线状态过滤 |
| `lensModel` | String \| Null | 按镜头型号过滤 |
| `libraryId` | UUID \| Null | 按媒体库 ID 过滤 |
| `make` | String | 按相机制造商过滤 |
| `model` | String \| Null | 按相机型号过滤 |
| `ocr` | String | 按 OCR 文本内容过滤 |
| `order` | AssetOrder | 排序顺序 |
| `originalFileName` | String | 按原始文件名过滤 |
| `originalPath` | String | 按原始文件路径过滤 |
| `page` | Number | 页码 |
| `personIds` | UUID[] | 按人物 ID 过滤 |
| `previewPath` | String | 按预览文件路径过滤 |
| `rating` | Number \| Null | 按评分过滤 [1-5]，null 表示未评分 |
| `size` | Number | 返回结果数量 |
| `state` | String \| Null | 按省/州名称过滤 |
| `tagIds` | UUID[] \| Null | 按标签 ID 过滤 |
| `takenAfter` | DateTime | 按拍摄日期过滤（之后） |
| `takenBefore` | DateTime | 按拍摄日期过滤（之前） |
| `thumbnailPath` | String | 按缩略图文件路径过滤 |
| `trashedAfter` | DateTime | 按回收日期过滤（之后） |
| `trashedBefore` | DateTime | 按回收日期过滤（之前） |
| `type` | AssetTypeEnum | 资源类型过滤 |
| `updatedAfter` | DateTime | 按更新日期过滤（之后） |
| `updatedBefore` | DateTime | 按更新日期过滤（之前） |
| `visibility` | AssetVisibility | 按可见性过滤 |
| `withDeleted` | Boolean | 包含已删除的资源 |
| `withExif` | Boolean | 在响应中包含 EXIF 数据 |
| `withPeople` | Boolean | 包含人物信息的资源 |
| `withStacked` | Boolean | 包含堆叠的资源 |

---

## 响应

| 字段 | 类型 |
|------|------|
| `albums` | SearchAlbumResponseDto |
| `assets` | SearchAssetResponseDto |


```markdown
# SearchAlbumResponseDto

## 属性

| 属性 | 类型 | 必填 | 描述 |
|------|------|------|------|
| count | Number | ✓ | Number of albums in this page（此页中的相册数量） |
| facets | SearchFacetResponseDto[] | ✓ | - |
| items | AlbumResponseDto[] | ✓ | - |
| total | Number | ✓ | Total number of matching albums（匹配的相册总数） |
```

```markdown
# AlbumResponseDto

## 属性列表

| Property | Type | Status | Description |
|----------|------|--------|-------------|
| albumName | String | * | Album name |
| albumThumbnailAssetId | String \| Null | * | Thumbnail asset ID |
| albumUsers | AlbumUserResponseDto[] | * | - |
| assetCount | Number | * | Number of assets |
| assets | AssetResponseDto[] | * | - |
| contributorCounts | ContributorCountResponseDto[] | - | - |
| createdAt | DateTime | * | Creation date |
| description | String | * | Album description |
| endDate | DateTime | - | End date (latest asset) |
| hasSharedLink | Boolean | * | Has shared link |
| id | String | * | Album ID |
| isActivityEnabled | Boolean | * | Activity feed enabled |
| lastModifiedAssetTimestamp | DateTime | - | Last modified asset timestamp |
| order | AssetOrder | - | Asset sort order |
| owner | UserResponseDto | * | - |
| ownerId | String | * | Owner user ID |
| shared | Boolean | * | Is shared album |
| startDate | DateTime | - | Start date (earliest asset) |
| updatedAt | DateTime | * | Last update date |
```

```markdown
# SearchAssetResponseDto
type: `object`

## 属性

| 属性名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| count | Number | ✓ | Number of assets in this page |
| facets | SearchFacetResponseDto[] | ✓ | - |
| items | AssetResponseDto[] | ✓ | - |
| nextPage | String \| Null | ✓ | Next page token |
| total | Number | ✓ | Total number of matching assets |
```

```markdown
# AssetResponseDto

对象模型属性列表：

| 属性 | 类型 | 状态 | 描述 |
|------|------|------|------|
| checksum* | String | | Base64 encoded SHA1 hash |
| createdAt* | DateTime | | The UTC timestamp when the asset was originally uploaded to Immich. |
| deviceAssetId* | String | | Device asset ID |
| deviceId* | String | | Device ID |
| duplicateId | String \| Null | | Duplicate group ID |
| duration* | String | | Video duration (for videos) |
| exifInfo | ExifResponseDto | | - |
| fileCreatedAt* | DateTime | | The actual UTC timestamp when the file was created/captured, preserving timezone information. This is the authoritative timestamp for chronological sorting within timeline groups. Combined with timezone data, this can be used to determine the exact moment the photo was taken. |
| fileModifiedAt* | DateTime | | The UTC timestamp when the file was last modified on the filesystem. This reflects the last time the physical file was changed, which may be different from when the photo was originally taken. |
| hasMetadata* | Boolean | | Whether asset has metadata |
| height* | Number \| Null | | Asset height |
| id* | String | | Asset ID |
| isArchived* | Boolean | | Is archived |
| isEdited* | Boolean | Beta | Is edited |
| isFavorite* | Boolean | | Is favorite |
| isOffline* | Boolean | | Is offline |
| isTrashed* | Boolean | | Is trashed |
| libraryId | UUID \| Null | Deprecated | Library ID |
| livePhotoVideoId | String \| Null | | Live photo video ID |
| localDateTime* | DateTime | | The local date and time when the photo/video was taken, derived from EXIF metadata. This represents the photographer's local time regardless of timezone, stored as a timezone-agnostic timestamp. Used for timeline grouping by "local" days and months. |
| originalFileName* | String | | Original file name |
| originalMimeType | String | | Original MIME type |
| originalPath* | String | | Original file path |
| owner | UserResponseDto | | - |
| ownerId* | String | | Owner user ID |
| people | PersonWithFacesResponseDto[] | | - |
| resized | Boolean | Deprecated | Is resized |
| stack | AssetStackResponseDto \| Null | | - |
| tags | TagResponseDto[] | | - |
| thumbhash* | String \| Null | | Thumbhash for thumbnail generation (base64) also used as the c query param for thumbnail cache busting. |
| type* | AssetTypeEnum | | Asset type |
| unassignedFaces | AssetFaceWithoutPersonResponseDto[] | | - |
| updatedAt* | DateTime | | The UTC timestamp when the asset record was last updated in the database. This is automatically maintained by the database and reflects when any field in the asset was last modified. |
| visibility* | AssetVisibility | | Asset visibility |
| width* | Number \| Null | | Asset width |
```

---

## 历史版本

- **v2** — 标记为 Status:Stable
- **v1** — 标记为 Status:Beta
- **v1** — 添加

---

## 导航

**上一个**: Search.searchLargeAssets
**下一个**: Search.searchPerson
