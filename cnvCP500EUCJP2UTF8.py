import os
import sys
import glob
import re
import struct
from typing import List, Dict, Any, Tuple, Optional, Callable

# --- Constants and Configuration ---

# Constants for process_binary_to_dat return codes
PROCESS_SUCCESS = 0  # 処理成功
PROCESS_ERROR_FILE_NOT_FOUND = -1  # バイナリファイルが見つからない
PROCESS_ERROR_COPY_NOT_FOUND = -2  # COPY句ファイルが見つからない
PROCESS_ERROR_COPY_PARSE = -3  # COPY句ファイルの解析エラー
PROCESS_ERROR_GENERAL = -4  # その他の処理エラー

# Default EBCDIC encoding - commonly used on IBM mainframes
# 特定のメインフレーム環境に応じて調整が必要な場合があります
# 例: 'cp500', 'cp1047', 'cp37' など
DEFAULT_EBCDIC_ENCODING = 'cp500'

# JEF specific character replacement (JEF to full-width space)
# JEF拡張における特定の文字の置換。
# これらのバイトシーケンスは実際のJEF実装で検証が必要な場合があります。
JEF_AT_MARK_PAIR_BYTES = b'\x42\x42'  # 例：全角のEUC-JP/JEFでのバイト表現
FULL_WIDTH_SPACE_UTF8 = '　'  # 全角スペースのUnicode文字
UNDEFINED_CHAR_REPLACEMENT = '★' # 未定義バイナリコードの置換文字

# File Extensions
BINARY_FILE_EXTENSION = 'bin'
COPY_FILE_EXTENSION = 'txt' # Assuming .txt for COPY files based on CPY_*.txt
DAT_FILE_EXTENSION = 'dat'

# Directory Names
INPUT_BINARY_DIR = './iBIN'
INPUT_CPY_DIR = './iCPY'
OUTPUT_DAT_DIR = './oDAT'

# Field Definition Keys
FIELD_NAME = 'name'
FIELD_TYPE = 'type'
FIELD_NUM_ATTRIBUTE = 'num_attribute'
FIELD_LENGTH = 'length'
FIELD_OFFSET = 'offset' # 0-based offset

# Delimiters
CSV_DELIMITER = ','
CSV_QUOTECHAR = '"'
CSV_ESCAPED_QUOTE = '""'

# Input file for character conversion map
# This will be prompted from the user when the script runs
# input_file_cnv = input('文字コード変換定義ファイルのパスを指定してください: ') # Moved to __main__ block

# --- Helper Functions ---

def get_filenames_with_extension(directory_path: str, extension: str) -> List[str]:
    """
    指定されたディレクトリ内で、特定の拡張子を持つファイル名のリストを取得する。
    ディレクトリが存在しない場合は空リストを返す。
    """
    if not os.path.isdir(directory_path):
        print(f"Warning: Directory not found: {directory_path}", file=sys.stderr)
        return []
    pattern = os.path.join(directory_path, f'*.{extension}')
    return glob.glob(pattern)

def check_file_exists(filepath: str) -> bool:
    """
    指定されたファイルパスが存在するか確認する。
    """
    return os.path.exists(filepath)

def get_filename_from_path(filepath: str) -> str:
    """
    ファイルパスからファイル名（拡張子含む）を取得する。
    """
    return os.path.basename(filepath)

# --- Configuration Loading ---

def load_conversion_map(file_path: str) -> Dict[str, str]:
    """
    文字コード変換定義ファイルを読み込み、マッピング辞書を生成する。
    ファイル形式: source_hex,target_hex (例: 4040,3000)
    """
    conversion_map: Dict[str, str] = {}
    if not check_file_exists(file_path):
        print(f"Error: Conversion map file not found: {file_path}", file=sys.stderr)
        return conversion_map # Return empty map if file not found

    try:
        with open(file_path, 'r', encoding='utf-16') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): # Skip empty lines and comments
                    continue
                parts = line.split(CSV_DELIMITER)
                if len(parts) == 2:
                    hex_src = parts[0].strip().upper()
                    hex_dst = parts[1].strip().upper()
                    # Basic validation for hex strings (optional but good practice)
                    if re.fullmatch(r'[0-9A-F]+', hex_src) and re.fullmatch(r'[0-9A-F]+', hex_dst):
                         conversion_map[hex_src] = hex_dst
                    else:
                         print(f"Warning: Skipping malformed line in conversion map file ({file_path}): {line}", file=sys.stderr)
                else:
                    print(f"Warning: Skipping malformed line in conversion map file ({file_path}): {line}", file=sys.stderr)
    except FileNotFoundError:
         # Already handled by check_file_exists, but keep for robustness
         print(f"Error: Conversion map file not found during loading: {file_path}", file=sys.stderr)
    except Exception as e:
        print(f"Error loading conversion map file {file_path}: {e}", file=sys.stderr)

    return conversion_map

# --- COPY File Parsing ---

def read_cpy_file(filepath: str) -> Optional[List[str]]:
    """
    COPY句ファイルを読み込み、各行を文字列のリストとして返す。
    ファイルが見つからない、または読み込みエラーの場合はNoneを返す。
    """
    if not check_file_exists(filepath):
        return None
    try:
        # UTF-8エンコーディングでファイルを開き、全行を読み込む
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        # 各行の先頭および末尾の空白文字（改行コード含む）を除去したリストを返す
        return [line.strip() for line in lines]
    except Exception as e:
        print(f"Error reading COPY file {filepath}: {e}", file=sys.stderr)
        return None

def parse_cpy_field_definitions(cpy_lines: List[str]) -> Tuple[Optional[int], Optional[List[Dict[str, Any]]], Optional[str]]:
    """
    COPY句の行リストを解析し、レコード長とフィールド定義のリストを抽出する。

    Args:
        cpy_lines: COPY句ファイルの行リスト。

    Returns:
        Tuple: (record_length, field_definitions, error_message)
               record_length: 整数 (成功時), None (エラー時)
               field_definitions: フィールド定義のリスト (成功時), None (エラー時)
               error_message: エラーメッセージ (エラー時), None (成功時)
    """
    if len(cpy_lines) < 3:
        return None, None, "Insufficient lines in COPY file for header and fields"

    # レコード長の解析
    try:
        record_length = int(cpy_lines[0])
        if record_length <= 0:
            return None, None, f"Invalid record length ({record_length}) in COPY file header"
    except ValueError:
        return None, None, "Invalid record length (not a number) in COPY file header"

    # フィールド定義の解析
    field_definitions: List[Dict[str, Any]] = []
    current_offset = 0 # 現在処理中のフィールドの開始オフセット (0-based)

    # COPY句ファイルの3行目（インデックス2）からフィールド定義を読み込む
    for i, line in enumerate(cpy_lines[2:], start=3): # enumerateのstartはログ出力用の行番号に使用
        parts = line.split(CSV_DELIMITER) # カンマで分割

        # 想定される列数（項目名,データ型,数値属性,バイト長,オフセット）は5つ
        # 数値属性が空欄の場合は4つになるため、そのケースも考慮し、空の要素を挿入
        if len(parts) == 4 and parts[2].strip() == "":
             parts = parts[:2] + [""] + parts[2:]

        if len(parts) != 5:
            print(f"Warning: Skipping malformed line in COPY file (line {i}): {line} - Expected 5 parts, got {len(parts)}", file=sys.stderr)
            continue

        try:
            # 各要素を取得し、前後の空白を除去
            field_name = parts[0].strip()
            data_type = parts[1].strip().upper() # データ型は大文字に変換
            num_attribute_str = parts[2].strip()
            byte_length = int(parts[3].strip())
            # COPY句ファイルに記載されているオフセット（1-based）を取得
            declared_offset_1based = int(parts[4].strip())

            # バイト長の検証
            if byte_length <= 0:
                print(f"Warning: Skipping field with invalid byte length ({byte_length}) in COPY file (line {i}): {line}", file=sys.stderr)
                continue

            # COPY句に記載されているオフセットと、プログラムが計算しているオフセット（1-basedに変換）を比較
            # 不一致は警告とするが、処理は続行し、計算したオフセットを使用する
            if declared_offset_1based != current_offset + 1:
                 print(f"Warning: Offset mismatch in COPY file (line {i}). Declared: {declared_offset_1based}, Calculated: {current_offset + 1}. Using calculated offset ({current_offset}).", file=sys.stderr)

            # このフィールド定義がレコード長の範囲内に収まっているか検証
            if current_offset + byte_length > record_length:
                 return None, None, f"Field definition exceeds record length (line {i}). Field '{field_name}' ends at byte {current_offset + byte_length}, Record length is {record_length}."

            # フィールド定義ディクショナリを作成しリストに追加
            field_definitions.append({
                FIELD_NAME: field_name,
                FIELD_TYPE: data_type,
                # 数値属性が空欄または数値でない場合はNoneとする
                FIELD_NUM_ATTRIBUTE: int(num_attribute_str) if num_attribute_str.isdigit() else None,
                FIELD_LENGTH: byte_length,
                FIELD_OFFSET: current_offset # 内部処理用の0-basedオフセットを格納
            })
            # 次のフィールドの開始オフセットを計算
            current_offset += byte_length

        except ValueError as e:
            print(f"Warning: Skipping malformed numeric value in COPY file (line {i}): {line} - {e}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"Warning: Skipping malformed line due to unexpected error in COPY file (line {i}): {line} - {e}", file=sys.stderr)
            continue

    # 全フィールド定義を解析した後の、計算された合計バイト長と宣言されたレコード長を比較
    if current_offset != record_length:
         print(f"Warning: Total calculated field length ({current_offset}) does not match declared record length ({record_length}) in COPY file.", file=sys.stderr)
         # この不一致は警告とし、処理は続行する

    if not field_definitions:
        return None, None, "No valid field definitions found in COPY file after parsing"

    return record_length, field_definitions, None

def get_digits_from_pic_type(pic_type_str: str) -> Tuple[int, int]:
    """
    PIC表現文字列から整数部と小数部の桁数を抽出する。
    例: '9(5)V9(2)' -> (5, 2), 'X(10)' -> (0, 0), 'P9(3)' -> (3, 0)
    """
    integer_digits = 0
    decimal_digits = 0

    # PIC表現を解析するための正規表現
    # PIC X, A などの文字型にはマッチしないように PIC 9, P9, S9, V9 などに限定
    # ^[PSV]*9 : 先頭はP, S, Vの0回以上の繰り返しと9
    # (?:\((\d+)\))? : 非捕捉グループで括弧内の数字にマッチ（整数部桁数 m）、オプション
    # (?:V9(?:\((\d+)\))?)? : 非捕捉グループで V9 とそれに続くオプションの括弧内数字にマッチ（小数部桁数 n）、全体がオプション
    match = re.match(r'^[PSV]*9(?:\((\d+)\))?(?:V9(?:\((\d+)\))?)?$', pic_type_str)

    if match:
        # group(1): 整数部の桁数 ('m')
        # group(2): 小数部の桁数 ('n')
        arg1 = match.group(1)
        arg2 = match.group(2)

        if arg1 is not None:
            integer_digits = int(arg1)
        # PICにVが含まれ、かつ小数部桁数nが指定されている場合
        if 'V' in pic_type_str and arg2 is not None:
            decimal_digits = int(arg2)

    return integer_digits, decimal_digits


# --- Encoding and Data Type Conversion Functions ---

def convert_ebcdic_to_string(ebcdic_bytes: bytes, ebcdic_encoding: str = DEFAULT_EBCDIC_ENCODING) -> str:
    """
    EBCDICバイト列をASCII互換Unicode文字列に変換する。
    NULL文字(0x00)の除去と、前後の空白除去を行う。
    変換できないEBCDIC文字は無視（除去）される。

    Args:
        ebcdic_bytes: EBCDICエンコードされたバイト列。
        ebcdic_encoding: 使用するEBCDICのエンコーディング名 (例: 'cp500')。

    Returns:
        変換されたASCII互換Unicode文字列。
        デコードに失敗した場合はエラー情報を含む文字列を返す。
    """
    # バイト列中のNULL文字(0x00)を除去する
    non_null_bytes = ebcdic_bytes.replace(b'\x00', b'')
    try:
        # 指定されたEBCDICエンコーディングでバイト列をデコードする。
        # errors='ignore' により、マップできないバイトは無視される。
        unicode_string = non_null_bytes.decode(ebcdic_encoding, errors='ignore')
        # デコード結果文字列の前後の空白を除去して返す
        return unicode_string.strip()
    except Exception as e:
        print(f"Warning: Could not decode EBCDIC bytes {ebcdic_bytes.hex()} with encoding {ebcdic_encoding}: {e}", file=sys.stderr)
        return f"DECODE_ERROR(EBCDIC):{ebcdic_bytes.hex()}"

def convert_jef_chars(jef_bytes: bytes, conversion_map: Dict[str, str]) -> str:
    """
    JEFバイト列を変換マップに従ってUnicode文字列に変換する。
    未定義コードは指定の置換文字で置き換える。
    JEF拡張における特定の文字の置換も行う。
    変換マップは JEF 2バイトの16進 -> Unicode コードポイントの16進 を想定。

    Args:
        jef_bytes: JEFエンコードされたバイト列。
        conversion_map: JEFバイト列の16進文字列からUnicodeコードポイントの16進文字列へのマッピング辞書。

    Returns:
        変換されたUnicode文字列。
    """
    result_text = ""
    # JEF拡張における特定の文字の置換。
    # JEF_AT_MARK_PAIR_BYTES が 全角のバイト列、FULL_WIDTH_SPACE_UTF8 が置換後の文字（全角スペース）
    # 置換後の全角スペースも euc_jp でエンコードされたバイト列に変換してreplaceする (元のコードの意図を維持)
    processed_bytes = jef_bytes.replace(JEF_AT_MARK_PAIR_BYTES, FULL_WIDTH_SPACE_UTF8.encode('euc_jp')) # エンコーディングは euc_jp でハードコード

    # 元コードの cnvJEF2UNI は2バイト固定で処理している。これを維持する。
    i = 0
    while i < len(processed_bytes):
        # 2バイトずつ処理（JEFは基本的に2バイト文字）
        if i + 1 < len(processed_bytes):
            byte_pair = processed_bytes[i:i+2]
            hex_value = byte_pair.hex().upper()
            converted_hex = conversion_map.get(hex_value)

            if converted_hex: # 変換マップに存在し、値がNoneでない場合
                try:
                    # 変換後の16進が複数バイトを表す場合（例: 複数バイトのUTF-8表現）を考慮
                    # 元コードはchr(int(converted_hex, 16))でUnicodeコードポイントに変換しているため、これを維持
                    # ただし、ターゲットHEXが空文字列の場合も未定義扱いとする (load_conversion_mapは空文字列を格納しないが念のため)
                    if len(converted_hex) > 0:
                         result_text += chr(int(converted_hex, 16))
                    else:
                         # ターゲットHEXが空文字列の場合は未定義文字を追加
                         print(f"Warning: Empty target hex found in conversion map for source '{hex_value}'. Using undefined character.", file=sys.stderr)
                         result_text += UNDEFINED_CHAR_REPLACEMENT
                except ValueError:
                    print(f"Warning: Invalid target hex '{converted_hex}' in conversion map for source '{hex_value}'. Using undefined character.", file=sys.stderr)
                    result_text += UNDEFINED_CHAR_REPLACEMENT
            else:
                # 変換マップに存在しない場合、未定義文字を追加
                result_text += UNDEFINED_CHAR_REPLACEMENT
            i += 2 # 2バイト進む
        else:
            # 最後の1バイトが残った場合（不完全なJEF文字など）
            print(f"Warning: Incomplete byte sequence at end of JEF data: {processed_bytes[i:].hex()}. Using undefined character.", file=sys.stderr)
            result_text += UNDEFINED_CHAR_REPLACEMENT
            i += 1 # 1バイト進む

    return result_text.strip() # 前後の空白を除去

def convert_ebcdic_zoned_decimal(ebcdic_bytes: bytes, decimal_digits: int, ebcdic_encoding: str = DEFAULT_EBCDIC_ENCODING) -> str:
    """
    ゾーン10進数 (EBCDIC) バイト列を小数点付き数値文字列に変換する。
    """
    # まず、EBCDICバイト列をそのままASCII互換文字列に変換する (数字以外の文字は無視される)
    numeric_string = convert_ebcdic_to_string(ebcdic_bytes, ebcdic_encoding).strip()

    # 空のフィールドだった場合は空文字列を返す
    if not numeric_string:
        return ""

    # 小数点以下の桁数が負数は通常ありえないが、防衛的に処理
    if decimal_digits < 0:
        print(f"Warning: Negative decimal places specified for zoned decimal: {decimal_digits}. Treating as 0.", file=sys.stderr)
        decimal_digits = 0

    actual_digits = len(numeric_string)

    # 小数点の挿入
    if decimal_digits == 0:
        return numeric_string
    elif actual_digits <= decimal_digits:
        # 例: データ '123', decimal_digits 5 -> "0.00123" のようになるケース (V9など)
        # specのV9ルール "先頭に "0." を付加" に従う。
        # 実際の桁数が小数部の桁数以下の場合、先頭に "0." を付加し、
        # 必要に応じて0を埋める。
        leading_zeros_count = decimal_digits - actual_digits
        leading_zeros = "0" * leading_zeros_count
        return "0." + leading_zeros + numeric_string
    else:
        # 小数点以下の桁数が実際の桁数より少ない場合、小数点以下の桁数を基準に挿入位置を計算
        # 例: データ '12345', decimal_digits 2 -> 挿入位置 5-2=3 -> "123.45"
        insert_position = actual_digits - decimal_digits
        return numeric_string[:insert_position] + '.' + numeric_string[insert_position:]


def convert_comp3_bytes(comp3_bytes: bytes, decimal_places: int) -> Optional[str]:
    """
    Packed decimal (COMP-3) バイト列を小数点付きまたは整数文字列に変換する汎用関数。
    decimal_places >= 0 の場合は小数点以下の桁数として扱い、
    decimal_places < 0 の場合は整数として扱う（小数点挿入なし）。

    Args:
        comp3_bytes: COMP-3 エンコードされたバイト列。
        decimal_places: 小数点以下の桁数。負数の場合は整数として扱う。

    Returns:
        変換された数値文字列、または不正なデータの場合はNone。
    """
    if not comp3_bytes:
        return None # Empty bytes

    num_bytes = len(comp3_bytes)
    if num_bytes == 0:
        return None

    # 末尾の1バイトを取得
    last_byte = comp3_bytes[-1]
    # 末尾バイトの下位4ビットが符号ニブル
    sign_nibble = last_byte & 0x0F
    # D (0x0D) が負の符号
    is_negative = (sign_nibble == 0x0D)

    digits = []
    # 末尾バイトを除いた各バイトを処理
    for byte in comp3_bytes[:-1]:
        # 上位4ビットと下位4ビットを抽出
        high_nibble = (byte >> 4) & 0x0F
        low_nibble = byte & 0x0F
        # 各ニブルが数字（0-9）であることを確認
        if not (0 <= high_nibble <= 9 and 0 <= low_nibble <= 9):
            print(f"Warning: Invalid digit nibble found in COMP-3 byte {byte:02x} (full data: {comp3_bytes.hex()})", file=sys.stderr)
            return None # Invalid digit
        # 数字ニブルを文字列としてリストに追加
        digits.append(str(high_nibble))
        digits.append(str(low_nibble))

    # 末尾バイトの上位4ビットを処理
    last_byte_high_nibble = (last_byte >> 4) & 0x0F
    # これも数字（0-9）であることを確認
    if not (0 <= last_byte_high_nibble <= 9):
        print(f"Warning: Invalid digit nibble found in last COMP-3 byte {last_byte:02x} (full data: {comp3_bytes.hex()})", file=sys.stderr)
        return None # Invalid digit
    # 数字ニブルを文字列としてリストに追加
    digits.append(str(last_byte_high_nibble))

    # 符号ニブルの妥当性チェック（C, D, F が一般的だが、A, B, E も符号として使われることがある）
    if sign_nibble not in [0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F]:
        print(f"Warning: Unusual sign nibble found in COMP-3 byte {last_byte:02x}: {sign_nibble:x} (full data: {comp3_bytes.hex()})", file=sys.stderr)
        # ここではC,Fを正、Dを負とし、その他は警告付きで正とみなす（元のコードの挙動を維持）
        if sign_nibble not in [0x0C, 0x0D, 0x0F]:
             print(f"Warning: Treating unusual sign nibble {sign_nibble:x} as positive.", file=sys.stderr)

    # 結合した数字文字列の先頭の0を除去する
    all_digits = "".join(digits).lstrip('0')

    # 結果が空文字列（元の値が0の場合など）の場合の処理
    if not all_digits:
        # 小数点以下の桁数が指定されている場合、 "0.00..." 形式で返す
        return "0." + "0" * decimal_places if decimal_places >= 0 and decimal_places > 0 else "0"

    # 小数点以下の桁数が指定されている場合（float変換）
    if decimal_places >= 0:
        total_digits = len(all_digits)
        if decimal_places == 0:
            # 小数点以下の桁数が0の場合、そのままの数字文字列
            numeric_string = all_digits
        elif decimal_places >= total_digits:
            # 小数点以下の桁数が全桁数以上の場合（例: PV9, PSV9）
            leading_zeros = "0" * (decimal_places - total_digits)
            numeric_string = "0." + leading_zeros + all_digits
        else:
            # 小数点以下の桁数が全桁数より少ない場合
            insert_position = total_digits - decimal_places
            numeric_string = all_digits[:insert_position] + '.' + all_digits[insert_position:]
    else:
         # decimal_places < 0 の場合、整数として扱う（小数点挿入なし）
         numeric_string = all_digits

    # 負の符号があれば "-" を付加して返す
    if is_negative:
        return "-" + numeric_string
    else:
        return numeric_string # 正の場合はそのまま返す


# --- Main Processing Class ---

class BinaryToDatConverter:
    """
    バイナリファイルをCOPY句ファイルに従って解析し、DATファイルに変換するクラス。
    """
    def __init__(self, binary_filepath: str, cpy_filepath: str, dat_filepath: str,
                 conversion_map: Dict[str, str], ebcdic_encoding: str = DEFAULT_EBCDIC_ENCODING):
        self.binary_filepath = binary_filepath
        self.cpy_filepath = cpy_filepath
        self.dat_filepath = dat_filepath
        self.conversion_map = conversion_map
        self.ebcdic_encoding = ebcdic_encoding
        self.record_length: Optional[int] = None
        self.field_definitions: Optional[List[Dict[str, Any]]] = None
        self.errors_encountered: int = 0

    def process(self) -> int:
        """
        変換処理を実行する。
        Returns: 処理結果を示すPROCESS_*定数。
        """
        bin_filename = get_filename_from_path(self.binary_filepath)
        cpy_filename = get_filename_from_path(self.cpy_filepath)
        print(f"Processing: {bin_filename}")
        print(f"  Using COPY: {cpy_filename}")

        # 入力ファイルの存在チェック
        if not check_file_exists(self.binary_filepath):
            print(f"Error: Binary file not found: {self.binary_filepath}", file=sys.stderr)
            return PROCESS_ERROR_FILE_NOT_FOUND

        if not check_file_exists(self.cpy_filepath):
            print(f"Error: COPY file not found: {self.cpy_filepath}", file=sys.stderr)
            return PROCESS_ERROR_COPY_NOT_FOUND

        # COPY句ファイルの読み込みと解析
        cpy_lines = read_cpy_file(self.cpy_filepath)
        if cpy_lines is None:
            print(f"Error: Failed to read COPY file: {self.cpy_filepath}", file=sys.stderr)
            return PROCESS_ERROR_COPY_PARSE

        self.record_length, self.field_definitions, error_msg = parse_cpy_field_definitions(cpy_lines)

        if self.record_length is None or self.field_definitions is None:
            print(f"Error: Failed to parse COPY file {self.cpy_filepath}: {error_msg}", file=sys.stderr)
            return PROCESS_ERROR_COPY_PARSE

        # バイナリデータの読み込みと変換処理
        records_processed = 0
        self.errors_encountered = 0 # Reset error counter for this file

        try:
            with open(self.binary_filepath, 'rb') as bin_f, \
                 open(self.dat_filepath, 'w', encoding='utf-8') as dat_f:

                # ヘッダー行を書き込む
                header_row = CSV_DELIMITER.join([f'{CSV_QUOTECHAR}{field[FIELD_NAME]}{CSV_QUOTECHAR}' for field in self.field_definitions])
                dat_f.write(header_row + '\n')

                # バイナリファイルをレコード長単位で順次読み込むループ
                while True:
                    record_bytes = bin_f.read(self.record_length)
                    if not record_bytes:
                        break # End of file

                    # 読み込んだバイト列の長さがレコード長と一致しない場合は警告
                    # これはファイルの末尾に不完全なレコードがある場合などに発生
                    if len(record_bytes) != self.record_length:
                        # bin_f.tell() は次に読み込む位置を返すため、不完全レコードの開始位置は tell() - len(record_bytes) となる
                        print(f"Warning: Incomplete record read at byte offset {bin_f.tell() - len(record_bytes)}. Expected {self.record_length} bytes, got {len(record_bytes)}. Skipping remaining data in this file.", file=sys.stderr)
                        self.errors_encountered += 1 # 不完全なレコードをエラーとしてカウント
                        # ファイルの残りのデータは処理しない
                        break # Stop processing this file

                    records_processed += 1
                    output_fields: List[str] = []

                    # このレコードから、フィールド定義に従って各フィールドデータを抽出・変換
                    for field_def in self.field_definitions:
                        field_data = record_bytes[field_def[FIELD_OFFSET] : field_def[FIELD_OFFSET] + field_def[FIELD_LENGTH]]
                        converted_value = self._convert_field(field_def, field_data)

                        # 変換結果をCSV形式で囲む処理
                        # Noneになるケースは_convert_field内でエラー文字列に変換済みだが、念のためstr()を使う
                        converted_value_str = str(converted_value)
                        # フィールド内のダブルクォートをエスケープ（""に置換）し、全体をダブルクォートで囲む
                        escaped_value = converted_value_str.replace(CSV_QUOTECHAR, CSV_ESCAPED_QUOTE)
                        output_fields.append(f'{CSV_QUOTECHAR}{escaped_value}{CSV_QUOTECHAR}')

                    # 1レコード分の全フィールドの変換結果をカンマで連結し、DATファイルに書き出す
                    dat_f.write(CSV_DELIMITER.join(output_fields) + '\n')

            print(f"  Finished processing. Records processed: {records_processed}. Errors encountered for this file: {self.errors_encountered}.")
            return PROCESS_SUCCESS if self.errors_encountered == 0 else PROCESS_ERROR_GENERAL

        except Exception as e:
            # ファイルを開く、読み書きするなどのより高レベルな処理で発生したエラー
            print(f"Fatal Error during file processing for {self.binary_filepath}: {e}", file=sys.stderr)
            return PROCESS_ERROR_GENERAL

    def _convert_field(self, field_def: Dict[str, Any], field_data: bytes) -> str:
        """
        単一のフィールドデータを、定義された型に従って変換する。
        Args:
            field_def: フィールド定義辞書。
            field_data: 変換対象のバイト列。
        Returns: 変換された文字列、またはエラー情報を含む文字列。
        """
        data_type = field_def[FIELD_TYPE]
        byte_length = field_def[FIELD_LENGTH]
        num_attribute = field_def[FIELD_NUM_ATTRIBUTE]
        field_name = field_def[FIELD_NAME]

        converted_value: Any = "" # Use Any for intermediate conversion results

        try:
            # データ型に応じた変換処理
            if data_type == 'X':
                # 文字列 (EBCDIC) の変換
                converted_value = convert_ebcdic_to_string(field_data, self.ebcdic_encoding)
            elif data_type == '9':
                 # ゾーン10進数 (符号なし整数)
                 # EBCDIC -> ASCII互換変換後、先頭の'0'を除去
                 converted_value = convert_ebcdic_to_string(field_data, self.ebcdic_encoding).lstrip('0')
                 # 全て0だった場合は "0" とする
                 if not converted_value: converted_value = '0'
            elif data_type == 'N':
                # 日本語文字列 (JEF含む) の変換
                # cnvJEF2UNI が2バイト固定処理であることから、バイト列全体をJEFとして変換マップで処理すると推測
                # 元コードの euc_jp_to_utf8 のロジックを参考に JEF文字変換関数を使用
                converted_value = convert_jef_chars(field_data, self.conversion_map)
            elif data_type.startswith('9') and 'V9' in data_type:
                # ゾーン10進数 (整数部・小数部指定あり) 例: 9(5)V9(2)
                # get_digits_from_pic_typeでPIC定義から整数部・小数部桁数を取得
                integer_digits, decimal_digits = get_digits_from_pic_type(data_type)
                # 小数点付きEBCDIC数値変換関数を呼び出す
                converted_value = convert_ebcdic_zoned_decimal(field_data, decimal_digits, self.ebcdic_encoding)
            elif data_type.startswith('V9'): # V9 without explicit lengths like V9(n)
                 # 先頭小数点のゾーン10進数 (V9単独)
                 # 元コードのV9処理はebcdic_to_ascii後に "0." を付加しており、decimal_digitsを考慮していない。
                 # 元コードの挙動を維持し、ebcdic_to_string後に "0." + numeric_str とする。
                 numeric_str = convert_ebcdic_to_string(field_data, self.ebcdic_encoding).lstrip('0')
                 if not numeric_str:
                      converted_value = "0.0" # Assumption: default for empty V9 is "0.0"
                 else:
                      converted_value = "0." + numeric_str # Follow original code logic for V9
                 # print(f"Warning: Assuming V9 conversion logic similar to original code (0. + digits) for field '{field_name}'. Check if this matches specification.", file=sys.stderr)


            # Packed decimal (COMP-3) 関連のデータ型
            # PIC表現 S9, P9, V9, PSV9 などが含まれる型を網羅的にチェック
            elif any(data_type.startswith(prefix) for prefix in ['P9', 'PS9', 'S9', 'SP9']) or data_type in ['PV9', 'PSV9']:
                 # get_digits_from_pic_typeでPIC定義から整数部・小数部桁数を取得
                 integer_digits_from_type, decimal_digits_from_type = get_digits_from_pic_type(data_type)

                 decimal_places_for_comp3: int # decimal_places >= 0 for float, < 0 for integer

                 # 小数点以下の桁数を特定するロジック
                 if 'V9' in data_type or data_type.startswith('PV9') or data_type.startswith('PSV9'):
                      # 小数点あり Packed Decimal
                      if data_type.startswith('PV9') or data_type.startswith('PSV9'):
                           # PV9, PSV9 の場合、小数点以下の桁数はCOPY句の「数値属性」列から取得する
                           if num_attribute is None:
                                print(f"Error: PV9/PSV9 type requires numeric attribute (decimal places) in COPY file (field {field_name}). Data: {field_data.hex()}", file=sys.stderr)
                                self.errors_encountered += 1
                                return f"ERROR(PV9/PSV9_NO_ATTR):{field_data.hex()}"
                           decimal_places_for_comp3 = num_attribute
                      else:
                           # P9(m)V9(n), PS9(m)V9(n) の場合、小数部桁数はPIC定義(n)から取得
                           decimal_places_for_comp3 = decimal_digits_from_type
                 else:
                      # 整数 Packed Decimal (S9, P9, PS9, SP9)
                      decimal_places_for_comp3 = -1 # Indicate integer conversion

                 converted_value = convert_comp3_bytes(field_data, decimal_places_for_comp3)

                 # COMP-3変換関数がNoneを返した場合（不正データなど）の処理
                 if converted_value is None:
                      self.errors_encountered += 1
                      return f"ERROR(COMP3_INVALID):{field_data.hex()}"

            else:
                # 定義されていない、または未対応のデータ型の場合
                print(f"Warning: Unsupported data type '{data_type}' for field '{field_name}'. Outputting raw hex: {field_data.hex()}", file=sys.stderr)
                self.errors_encountered += 1
                return f"UNSUPPORTED_TYPE({data_type}):{field_data.hex()}"

            # 変換結果が文字列でない場合、文字列に変換
            return str(converted_value)

        except Exception as e:
            # 個別のフィールド変換中に予期せぬ例外が発生した場合
            print(f"Error: Exception during conversion for field '{field_name}' (type {data_type}). Data: {field_data.hex()} - {e}", file=sys.stderr)
            self.errors_encountered += 1
            return f"CONVERSION_ERROR: {e}"


# --- Main Execution ---

if __name__ == "__main__":
    # 文字コード変換定義ファイルのパスを指定
    input_file_cnv = input('文字コード変換定義ファイルのパスを指定してください: ')

    # 文字コード変換マップの読み込み
    conversion_map = load_conversion_map(input_file_cnv)
    if not conversion_map:
        print("Error: Conversion map is empty or failed to load. Exiting.", file=sys.stderr)
        sys.exit(PROCESS_ERROR_GENERAL)

    # 出力ディレクトリが存在しない場合は自動作成する
    if not os.path.exists(OUTPUT_DAT_DIR):
        print(f"Creating output directory: {OUTPUT_DAT_DIR}")
        os.makedirs(OUTPUT_DAT_DIR)

    # 入力バイナリディレクトリから指定拡張子を持つファイルリストを取得
    binary_files = get_filenames_with_extension(INPUT_BINARY_DIR, BINARY_FILE_EXTENSION)

    # 処理対象ファイルが見つからなかった場合
    if not binary_files:
        print(f"No .{BINARY_FILE_EXTENSION} files found in {INPUT_BINARY_DIR}. Exiting.", file=sys.stderr)
        sys.exit(0) # 警告を表示して正常終了

    # 処理するファイルの総数を取得
    total_files = len(binary_files)
    # 処理成功ファイル数、失敗ファイル数を初期化
    successful_files = 0
    failed_files = 0

    print(f"Found {total_files} binary files to process.")

    # 各バイナリファイルに対して処理を行うループ
    for bin_filepath in binary_files:
        bin_filename = get_filename_from_path(bin_filepath)
        bin_base_name = os.path.splitext(bin_filename)[0]

        # 対応するCOPY句ファイル名と出力DATファイル名を生成
        # COPYファイル名の生成ルールを元コードから踏襲 (CPY_ + base_name + .txt)
        cpy_filename = f'CPY_{bin_base_name}.{COPY_FILE_EXTENSION}'
        cpy_filepath = os.path.join(INPUT_CPY_DIR, cpy_filename)

        # 出力DATファイル名の生成ルールを元コードから踏襲 (LOAD_ + base_name + .dat)
        dat_filename = f'LOAD_{bin_base_name}.{DAT_FILE_EXTENSION}'
        dat_filepath = os.path.join(OUTPUT_DAT_DIR, dat_filename)

        # Converterクラスのインスタンスを作成し、処理を実行
        converter = BinaryToDatConverter(
            binary_filepath=bin_filepath,
            cpy_filepath=cpy_filepath,
            dat_filepath=dat_filepath,
            conversion_map=conversion_map,
            ebcdic_encoding=DEFAULT_EBCDIC_ENCODING # エンコーディング設定を渡す
        )
        result_code = converter.process()

        # 変換処理の結果コードに応じてカウントを更新し、ステータスを表示
        if result_code == PROCESS_SUCCESS:
            successful_files += 1
            print(f"Status: SUCCESS - {bin_filename}\n")
        else:
            failed_files += 1
            # エラーコードに応じたステータス文字列を設定
            status = "ERROR"
            if result_code == PROCESS_ERROR_FILE_NOT_FOUND: status = "ERROR (BIN_NOT_FOUND)"
            elif result_code == PROCESS_ERROR_COPY_NOT_FOUND: status = "ERROR (CPY_NOT_FOUND)"
            elif result_code == PROCESS_ERROR_COPY_PARSE: status = "ERROR (CPY_PARSE_ERROR)"
            elif result_code == PROCESS_ERROR_GENERAL: status = "ERROR (PROCESSING_ERROR)" # General processing error (e.g., conversion issues within a file)
            print(f"Status: {status} - {bin_filename}\n", file=sys.stderr)

    # 全ファイルの処理完了後の集計結果を表示
    print("--- Processing Summary ---")
    print(f"Total files attempted: {total_files}")
    print(f"Successful conversions: {successful_files}")
    print(f"Failed conversions: {failed_files}")

    # 最終的なプログラムの終了コードを設定
    sys.exit(1 if failed_files > 0 else 0)
