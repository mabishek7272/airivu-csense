import { ImageManipulator, SaveFormat } from 'expo-image-manipulator';

/** Downscales to `width` and re-encodes as JPEG, so what is uploaded is small and always in
 *  a format the server accepts (a camera roll photo may be HEIC). */
export async function prepareForUpload(uri: string, width: number, compress: number): Promise<string> {
  const context = ImageManipulator.manipulate(uri);
  const rendered = await context.resize({ width }).renderAsync();
  const saved = await rendered.saveAsync({ format: SaveFormat.JPEG, compress });
  return saved.uri;
}
